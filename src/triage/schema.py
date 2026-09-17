"""The report contract.

Forcing structured output does two things: it makes the Jira comment
deterministic to render, and it makes the agent commit to a confidence level
and an explicit unverified-assumptions list instead of writing a confident
paragraph that reads like a conclusion but isn't one.
"""

from __future__ import annotations

from typing import Any

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "verdict",
        "confidence",
        "one_line_summary",
        "root_cause",
        "evidence",
        "blast_radius",
        "unverified",
        "next_actions",
    ],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [
                "root_cause_identified",
                "narrowed_not_confirmed",
                "not_reproducible_from_data",
                "insufficient_information",
                "not_a_bug",
            ],
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "high only when a code path, a log line and a data "
            "record all agree.",
        },
        "one_line_summary": {"type": "string", "maxLength": 200},
        "root_cause": {
            "type": "object",
            "additionalProperties": False,
            "required": ["explanation", "component"],
            "properties": {
                "explanation": {"type": "string"},
                "component": {"type": "string"},
                "code_location": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "symbol": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["file", "why"],
                },
                "suggested_fix": {"type": "string"},
            },
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "claim", "detail"],
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["new_relic", "databricks", "code", "jira"],
                    },
                    "claim": {"type": "string"},
                    "detail": {"type": "string"},
                    "query_or_path": {"type": "string"},
                },
            },
        },
        "blast_radius": {
            "type": "object",
            "additionalProperties": False,
            "required": ["described"],
            "properties": {
                "described": {"type": "string"},
                "affected_record_count": {"type": "integer"},
                "affected_window": {"type": "string"},
                "measured_by": {"type": "string"},
            },
        },
        "unverified": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Assumptions not backed by a query. Be honest here.",
        },
        "next_actions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "owner_hint"],
                "properties": {
                    "action": {"type": "string"},
                    "owner_hint": {"type": "string"},
                    "urgency": {
                        "type": "string",
                        "enum": ["now", "this_sprint", "backlog"],
                    },
                },
            },
        },
    },
}


def render_markdown(report: dict[str, Any], trace_md: str, run_id: str) -> str:
    """Report -> the Jira comment body."""
    r = report
    rc = r.get("root_cause", {}) or {}
    loc = rc.get("code_location") or {}
    br = r.get("blast_radius", {}) or {}

    lines = [
        f"*Automated triage* — verdict: `{r['verdict']}`, "
        f"confidence: `{r['confidence']}`",
        "",
        f"**{r['one_line_summary']}**",
        "",
        "### Root cause",
        rc.get("explanation", "_not determined_"),
    ]
    if rc.get("component"):
        lines.append(f"\nComponent: `{rc['component']}`")
    if loc:
        where = loc["file"] + (f":{loc['line']}" if loc.get("line") else "")
        lines.append(f"Code: `{where}`" + (f" — {loc.get('why','')}" if loc.get("why") else ""))
    if rc.get("suggested_fix"):
        lines += ["", "### Suggested fix", rc["suggested_fix"]]

    lines += ["", "### Evidence"]
    for e in r.get("evidence", []):
        q = f"\n  `{e['query_or_path']}`" if e.get("query_or_path") else ""
        lines.append(f"- **[{e['source']}]** {e['claim']} — {e['detail']}{q}")

    lines += ["", "### Blast radius", br.get("described", "_unknown_")]
    if br.get("affected_record_count") is not None:
        lines.append(
            f"Measured: {br['affected_record_count']} records "
            f"({br.get('affected_window','window unspecified')}) "
            f"via {br.get('measured_by','')}"
        )

    if r.get("unverified"):
        lines += ["", "### Not verified"]
        lines += [f"- {u}" for u in r["unverified"]]

    lines += ["", "### Suggested next actions"]
    for a in r.get("next_actions", []):
        lines.append(
            f"- [{a.get('urgency','this_sprint')}] {a['action']} "
            f"— _{a['owner_hint']}_"
        )

    lines += [
        "",
        "### Queries run",
        trace_md,
        "",
        f"_run {run_id} · generated by incident_iq · "
        "verify before acting on it_",
    ]
    return "\n".join(lines)
