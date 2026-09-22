"""Plain-English rewrite of the structured report, for a non-engineer approver.

A second, tool-free model call — the structured report already has the real
analysis; this just translates it. Deliberately uses the *primary* model, not
`fallback_model`: at least one Bedrock inference profile seen in testing 500s
on the SDK's internal session-title request when used here, which surfaces as
a bogus "malformed input request" response to our actual query too.
"""

from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from .config import Settings

_SYSTEM_PROMPT = (
    "You rewrite an incident triage report for a non-engineer approver, as "
    "plain conversational prose - output ONLY that prose, nothing else. "
    "The report has a `plain_language` block already written for this "
    "audience: use it as your source of truth and do not contradict it or "
    "re-derive the diagnosis from the technical fields. Match its hedging "
    "exactly - if it says a cause is unconfirmed, yours must too. "
    "Hard rules: no markdown at all (no headers, no **bold**, no bullet "
    "points or numbered lists, no backticks); write full sentences in "
    "paragraphs instead. No jargon, no file paths, no code. 4-6 sentences: "
    "what broke, why (in plain terms), how confident we are, and what happens "
    "next. If the verdict is not root_cause_identified, say clearly that this "
    "is a partial finding, not a confirmed fix."
)

# Safety net: strip markdown the model emits anyway before it reaches the UI.
_BOLD_ITALIC = re.compile(r"\*\*(.*?)\*\*|\*(.*?)\*|__(.*?)__|_(.*?)_")
_CODE = re.compile(r"`([^`]*)`")
_LINE_PREFIX = re.compile(r"^(#{1,6}\s+|[-*+]\s+|\d+\.\s+)", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    text = _BOLD_ITALIC.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    text = _CODE.sub(r"\1", text)
    text = _LINE_PREFIX.sub("", text)
    return text.strip()


async def simplify(report: dict[str, Any], settings: Settings) -> str:
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM_PROMPT,
        disallowed_tools=[
            "Write", "Edit", "MultiEdit", "Bash", "NotebookEdit",
            "Read", "Grep", "Glob", "WebFetch", "WebSearch",
        ],
        setting_sources=[],
        model=settings.agent.model,
        max_turns=1,
        max_budget_usd=0.20,
        env={"API_TIMEOUT_MS": "60000", **settings.agent.provider_env()},
    )

    narration: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"Rewrite this report:\n\n{report}")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        narration.append(block.text)

    return _strip_markdown("\n".join(narration)) or "(no summary produced)"
