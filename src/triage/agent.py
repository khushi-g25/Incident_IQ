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

import contextvars
import json
import sys
import time
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
from .prevention import build_document, write_document
from .redact import Redactor
from .schema import (
    REPORT_SCHEMA,
    normalize_report,
    render_markdown,
    validate_report,
)
from .tools import build_tools
from .trace import RunTrace

SYSTEM_PROMPT_PATH = REPO_ROOT / "prompts/system.md"
TEXT_ATTACHMENT_TYPES = ("text/", "application/json", "application/xml")

# Optional per-task sink for progress lines (e.g. a UI streaming them to a
# browser). Unset by default, so the CLI/webhook paths are unaffected; a
# caller sets it for the duration of one `triage()` call via `contextvars`,
# which asyncio.create_task() already isolates per task.
LOG_SINK: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "LOG_SINK", default=None
)


def _log(msg: str) -> None:
    """Lightweight progress line to stderr, timestamped, flushed immediately."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    sink = LOG_SINK.get()
    if sink is not None:
        sink.append(line)


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
    prevention_doc: str | None = None
    prevention_path: Path | None = None


async def triage(
    ticket_key: str,
    settings: Settings,
    post: bool | None = None,
    extra_context: str | None = None,
) -> TriageOutcome:
    t0 = time.time()
    _log(f"🚀 starting triage for {ticket_key} (model={settings.agent.model})")
    jira = JiraClient(settings.jira)
    redactor = Redactor()
    trace = RunTrace(ticket=ticket_key)

    # ---- Phase 1: intake -------------------------------------------------
    _log(f"📥 [intake] fetching Jira issue {ticket_key} ...")
    issue = jira.get_issue(ticket_key)
    _log(f"📥 [intake] fetched issue: {issue.summary!r} "
         f"({len(issue.attachments)} attachment(s))")
    attach_text = _attachment_text(jira, issue)
    raw_text = issue.as_prompt_text() + attach_text
    _log("🔎 [intake] extracting entities/services from ticket text ...")
    brief = extract_brief(
        key=issue.key,
        summary=issue.summary,
        text=raw_text,
        entity_patterns=settings.playbook.entity_patterns,
        services=settings.playbook.services,
    )
    _log(f"🔎 [intake] triageable={brief.is_triageable} in {time.time()-t0:.1f}s")

    # ---- Phase 2: gate ---------------------------------------------------
    if not brief.is_triageable:
        reason = (
            "No error signature, identifier or timestamp found in the ticket. "
            "Nothing to correlate against logs or data — needs more detail from "
            "the reporter before an automated pass is worth running."
        )
        _log(f"⛔ [gate] rejecting ticket: {reason}")
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
    _log("✅ [gate] ticket passed — proceeding to agent")

    # ---- Phase 3: agent loop ---------------------------------------------
    _log("🛠️  [setup] building tools (New Relic, Jira search) ...")
    server, allowed = build_tools(
        settings, redactor, trace, default_window=brief.nrql_window()
    )
    repo_roots = settings.playbook.repo_paths()
    _log(f"🛠️  [setup] tools ready: {', '.join(a.split('__')[-1] for a in allowed)}")

    options = ClaudeAgentOptions(
        system_prompt={
            "type": "file",
            "path": str(SYSTEM_PROMPT_PATH),
        },
        mcp_servers={"triage": server},
        strict_mcp_config=True,
        allowed_tools=allowed,
        # Offering Read/Grep/Glob with no clone on disk cost real turns: the
        # playbook's repo_paths are authored on another machine, so the agent
        # globbed a nonexistent directory before falling back to gh_*.
        disallowed_tools=[
            "Write", "Edit", "MultiEdit", "Bash", "NotebookEdit",
            *([] if repo_roots else ["Read", "Grep", "Glob"]),
        ],
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
        env={"API_TIMEOUT_MS": "180000", **settings.agent.provider_env()},
    )

    prompt = _build_prompt(redactor.scrub(raw_text), brief, settings, extra_context)

    report: dict[str, Any] | None = None
    narration: list[str] = []
    prev_tool_count = 0
    prevention_doc: str | None = None
    prevention_path: Path | None = None

    _log(f"🤖 [agent] querying {settings.agent.model} "
         f"(max_turns={settings.agent.max_turns}, budget=${settings.agent.max_budget_usd}) ...")
    agent_t0 = time.time()
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        narration.append(block.text)
                        preview = block.text.strip().replace("\n", " ")[:160]
                        _log(f"🤖 [agent] says: {preview}")
                # surface any new tool calls recorded in the trace since last check
                if len(trace.entries) > prev_tool_count:
                    for entry in trace.entries[prev_tool_count:]:
                        status = f"error: {entry.error}" if entry.error else (entry.result_summary or "ok")
                        _log(f"🔧 [tool] {entry.tool} ({entry.elapsed_ms}ms) -> {status}")
                    prev_tool_count = len(trace.entries)
            elif isinstance(message, ResultMessage):
                trace.cost_usd = message.total_cost_usd or 0.0
                report = _extract_report(message)
                _log(f"🏁 [agent] finished: cost=${trace.cost_usd:.4f} "
                     f"report_produced={report is not None}")

    trace.turns = len(trace.entries)
    _log(f"⏱️  [agent] loop took {time.time()-agent_t0:.1f}s, "
         f"{trace.turns} tool call(s) total")

    # ---- Phase 4: render + hand off --------------------------------------
    _log("📝 [render] building final markdown report ...")
    if report is None:
        markdown = (
            "*Automated triage did not produce a structured report.*\n\n"
            "Model narration:\n\n" + "\n".join(narration)[-4000:]
            + "\n\n### Queries run\n" + trace.evidence_markdown()
        )
    else:
        report = normalize_report(report)
        report, caveats = validate_report(report, trace.query_stats)
        for c in caveats:
            _log(f"⚠️  [validate] {c}")
        markdown = redactor.unscrub(
            render_markdown(
                report, trace.evidence_markdown(), trace.run_id, caveats,
                logs_md=trace.logs_checked_markdown(),
            )
        )
        try:
            doc = redactor.unscrub(
                build_document(
                    ticket_key, report, trace.run_id, trace.evidence_markdown()
                )
            )
            prevention_path = write_document(
                settings.run_dir, ticket_key, trace.run_id, doc
            )
            prevention_doc = doc
            _log(f"📄 [prevent] wrote prevention notes to {prevention_path}")
        except OSError:  # noqa: BLE001 - a failed write must not lose the report
            _log("⚠️  [prevent] could not write the prevention document")

    should_post = (not settings.dry_run) if post is None else post
    if should_post:
        _log(f"📤 [post] posting comment to {ticket_key} ...")
    else:
        _log("📤 [post] dry run — not posting to Jira")
    comment_id = jira.add_comment(ticket_key, markdown) if should_post else None

    _log(f"✅ done with {ticket_key} in {time.time()-t0:.1f}s total "
         f"(cost=${trace.cost_usd:.4f})")

    return TriageOutcome(
        ticket=ticket_key,
        run_id=trace.run_id,
        brief=brief,
        report=report,
        markdown=markdown,
        trace_path=trace.save(settings.run_dir),
        cost_usd=trace.cost_usd,
        posted_comment_id=comment_id,
        prevention_doc=prevention_doc,
        prevention_path=prevention_path,
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
            _log(f"⚠️  [intake] failed to fetch attachment {name!r}")
            continue
        _log(f"📎 [intake] fetched attachment {name!r} ({len(body)} chars)")
        chunks.append(f"\n## Attachment: {name}\n```\n{body[:60_000]}\n```")
    return "".join(chunks)


def _build_prompt(
    ticket_text: str,
    brief: TicketBrief,
    settings: Settings,
    extra_context: str | None = None,
) -> str:
    pb = settings.playbook
    svc_lines = [
        f"- {name}: repo at {meta.get('repo_path','?')}, "
        f"NR appName `{meta.get('nr_app_name', name)}`"
        for name, meta in pb.services.items()
        if not name.startswith("_")
    ]
    extra = f"\n## Additional context from the requester\n{extra_context}\n" if extra_context else ""
    return f"""Triage this ticket.

{ticket_text}

{brief.as_prompt_text()}
{extra}
## Services in scope
{chr(10).join(svc_lines)}

## Playbook notes
{pb.triage_notes or "(none)"}

Work the method in order. Budget: {settings.agent.max_turns} turns. Finish with
the structured report."""
