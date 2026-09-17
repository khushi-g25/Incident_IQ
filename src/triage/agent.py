"""Orchestration.

Phases:
  1. Deterministic intake  — fetch ticket, pull log attachments, extract entities.
  2. Gate                  — bounce tickets with no machine-usable signal.
  3. Agent loop            — Agent SDK drives tools until it has a report.
  4. Render + hand off     — structured report -> markdown -> Jira draft comment.

The agent loop is the only non-deterministic part, and it is bounded on three
axes: turns, dollars, and tool calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .clients.jira import JiraClient, JiraIssue
from .config import REPO_ROOT, Settings
from .extract import TicketBrief, extract_brief
from .guardrails import build_hooks
from .redact import Redactor
from .schema import REPORT_SCHEMA, render_markdown
from .tools import build_tools
from .trace import RunTrace

SYSTEM_PROMPT_PATH = REPO_ROOT / "prompts/system.md"
TEXT_ATTACHMENT_TYPES = ("text/", "application/json", "application/xml")


@dataclass
class TriageOutcome:
    ticket: str
    run_id: str
    brief: TicketBrief
    report: dict[str, Any] | None
    markdown: str
    trace_path: Path
    cost_usd: float
    posted_comment_id: str | None = None
    skipped_reason: str | None = None


async def triage(
    ticket_key: str,
    settings: Settings,
    post: bool | None = None,
) -> TriageOutcome:
    jira = JiraClient(settings.jira)
    redactor = Redactor()
    trace = RunTrace(ticket=ticket_key)

    # ---- Phase 1: intake -------------------------------------------------
    issue = jira.get_issue(ticket_key)
    raw_text = issue.as_prompt_text() + _attachment_text(jira, issue) #fetches the ticket text and the attachment text
    brief = extract_brief(
        key=issue.key,
        summary=issue.summary,
        text=raw_text,
        entity_patterns=settings.playbook.entity_patterns,
        services=settings.playbook.services,
    )

    # ---- Phase 2: gate ---------------------------------------------------
    if not brief.is_triageable:
        reason = (
            "No error signature, identifier or timestamp found in the ticket. "
            "Nothing to correlate against logs or data — needs more detail from "
            "the reporter before an automated pass is worth running."
        )
        return TriageOutcome(
            ticket=ticket_key,
            run_id=trace.run_id,
            brief=brief,
            report=None,
            markdown=f"*Automated triage skipped.* {reason}",
            trace_path=trace.save(settings.run_dir),
            cost_usd=0.0,
            skipped_reason=reason,
        )

    # ---- Phase 3: agent loop ---------------------------------------------
    server, allowed = build_tools(settings, redactor, trace)
    repo_roots = settings.playbook.repo_paths()

    options = ClaudeAgentOptions(
        system_prompt={
            "type": "file",
            "path": str(SYSTEM_PROMPT_PATH),
        },
        mcp_servers={"triage": server},
        strict_mcp_config=True,
        allowed_tools=allowed,
        disallowed_tools=["Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"],
        hooks=build_hooks(trace, repo_roots),
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=repo_roots[0] if repo_roots else None,
        add_dirs=repo_roots[1:],
        model=settings.agent.model,
        fallback_model=settings.agent.fallback_model,
        effort=settings.agent.effort,
        max_turns=settings.agent.max_turns,
        max_budget_usd=settings.agent.max_budget_usd,
        output_format={"type": "json_schema", "schema": REPORT_SCHEMA},
        env={"API_TIMEOUT_MS": "180000"},
    )

    prompt = _build_prompt(redactor.scrub(raw_text), brief, settings)

    report: dict[str, Any] | None = None
    narration: list[str] = []

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        narration.append(block.text)
            elif isinstance(message, ResultMessage):
                trace.cost_usd = message.total_cost_usd or 0.0
                report = _extract_report(message)

    trace.turns = len(trace.entries)

    # ---- Phase 4: render + hand off --------------------------------------
    if report is None:
        markdown = (
            "*Automated triage did not produce a structured report.*\n\n"
            "Model narration:\n\n" + "\n".join(narration)[-4000:]
            + "\n\n### Queries run\n" + trace.evidence_markdown()
        )
    else:
        markdown = redactor.unscrub(
            render_markdown(report, trace.evidence_markdown(), trace.run_id)
        )

    should_post = (not settings.dry_run) if post is None else post
    comment_id = jira.add_comment(ticket_key, markdown) if should_post else None

    return TriageOutcome(
        ticket=ticket_key,
        run_id=trace.run_id,
        brief=brief,
        report=report,
        markdown=markdown,
        trace_path=trace.save(settings.run_dir),
        cost_usd=trace.cost_usd,
        posted_comment_id=comment_id,
    )


def _extract_report(message: ResultMessage) -> dict[str, Any] | None:
    """Structured output arrives on the ResultMessage; fall back to parsing."""
    for attr in ("parsed_output", "structured_output"):
        val = getattr(message, attr, None)
        if isinstance(val, dict):
            return val
    raw = getattr(message, "result", None)
    if isinstance(raw, str):
        text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _attachment_text(jira: JiraClient, issue: JiraIssue, cap: int = 3) -> str:
    """Log files attached to the ticket are usually the richest signal present."""
    chunks = []
    for att in issue.attachments[:cap]:
        mime = att.get("mimeType") or ""
        name = att["filename"]
        if not (mime.startswith(TEXT_ATTACHMENT_TYPES) or name.endswith((".log", ".txt", ".json"))):
            continue
        try:
            body = jira.fetch_attachment_text(att["content"])
        except Exception:  # noqa: BLE001 - a bad attachment must not kill the run
            continue
        chunks.append(f"\n## Attachment: {name}\n```\n{body[:60_000]}\n```")
    return "".join(chunks)


def _build_prompt(ticket_text: str, brief: TicketBrief, settings: Settings) -> str:
    pb = settings.playbook
    svc_lines = [
        f"- {name}: repo at {meta.get('repo_path','?')}, "
        f"NR appName `{meta.get('nr_app_name', name)}`"
        for name, meta in pb.services.items()
        if not name.startswith("_")
    ]
    return f"""Triage this ticket.

{ticket_text}

{brief.as_prompt_text()}

## Services in scope
{chr(10).join(svc_lines)}

## Playbook notes
{pb.triage_notes or "(none)"}

Work the method in order. Budget: {settings.agent.max_turns} turns. Finish with
the structured report."""
