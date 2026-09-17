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

        if len(trace.entries) >= max_tool_calls:
            return _deny(
                f"Tool-call budget of {max_tool_calls} reached. Write up what you "
                "have, and state explicitly what is still unverified."
            )

        if name in ("Read", "Grep", "Glob"):
            target = args.get("file_path") or args.get("path") or ""
            if target and roots:
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
