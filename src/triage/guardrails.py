"""Guardrails, implemented as Agent SDK hooks.

`can_use_tool` only fires when the permission flow falls through to a prompt, so
anything auto-approved by `allowed_tools` never reaches it. PreToolUse hooks fire
on *every* call, which is what you want for a hard gate.

Three gates:
  - deny any tool that could mutate state (Write/Edit/Bash/NotebookEdit)
  - deny reads that wander outside the configured repo roots
  - stop the run when the tool-call budget is exhausted, so a confused agent
    cannot loop on expensive Databricks queries
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

from .trace import RunTrace

MUTATING_TOOLS = {
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput",
    "KillShell", "WebFetch",
}

# The SDK's own report-submission call goes through this same hook. Blocking it
# once the budget is spent defeats the "write up what you have" instruction —
# there'd be nowhere left for that write-up to go.
FINALIZATION_TOOLS = {"StructuredOutput"}

# These record themselves in the trace, with a real result summary and an
# evidence link, once they return. Logging them here as well double-counted
# every call: it burned the tool-call budget at twice the intended rate and
# filled the Jira evidence table with "(pre-flight ok)" rows that cite nothing.
SELF_TRACING_PREFIX = "mcp__triage__"

# Bookkeeping calls that are not evidence and should not be charged to the
# investigation budget.
UNBILLED_TOOLS = {"ToolSearch", "TodoWrite"}


def build_hooks(
    trace: RunTrace,
    repo_roots: list[str],
    max_tool_calls: int = 60,
) -> dict[str, list[HookMatcher]]:
    roots = [Path(r).resolve() for r in repo_roots]

    async def pre_tool_use(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        name = input_data.get("tool_name", "")
        args = input_data.get("tool_input", {}) or {}

        if name in MUTATING_TOOLS:
            return _deny(
                f"{name} is disabled. This agent diagnoses; it does not change "
                "anything. Report the fix as a suggested diff in your findings."
            )

        billable = [
            e for e in trace.entries if e.tool not in UNBILLED_TOOLS
        ]
        if len(billable) >= max_tool_calls and name not in FINALIZATION_TOOLS:
            return _deny(
                f"Tool-call budget of {max_tool_calls} reached. Write up what you "
                "have, and state explicitly what is still unverified."
            )

        if name in ("Read", "Grep", "Glob"):
            if not roots:
                return _deny(
                    "No service repository is cloned on this machine, so there is "
                    "nothing local to read. Use gh_search_code / gh_file to read "
                    "the service's default branch from GitHub instead."
                )
            target = args.get("file_path") or args.get("path") or ""
            if target:
                try:
                    resolved = Path(target).resolve()
                except OSError:
                    return _deny(f"Unreadable path: {target}")
                if not any(
                    resolved == r or r in resolved.parents for r in roots
                ):
                    return _deny(
                        f"{target} is outside the configured repo roots "
                        f"({[str(r) for r in roots]})."
                    )

        if not name.startswith(SELF_TRACING_PREFIX) and name not in UNBILLED_TOOLS:
            trace.add(name, args, result_summary="(pre-flight ok)")
        return {}

    return {"PreToolUse": [HookMatcher(hooks=[pre_tool_use])]}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
