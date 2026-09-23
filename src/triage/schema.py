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
        "signals_confirmed",
        "one_line_summary",
        "plain_language",
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
        "signals_confirmed": {
            "type": "object",
            "additionalProperties": False,
            "required": ["logs", "code", "data"],
            "description": "Which of the three legs of the diagnosis you "
            "actually stood on. 'absent' means you queried the right place and "
            "found nothing; 'not_checked' means you never successfully queried "
            "it. Be strict: this is cross-checked against the run trace.",
            "properties": {
                "logs": {
                    "type": "string",
                    "enum": ["confirmed", "absent", "not_checked"],
                },
                "code": {
                    "type": "string",
                    "enum": ["confirmed", "absent", "not_checked"],
                },
                "data": {
                    "type": "string",
                    "enum": ["confirmed", "absent", "not_checked"],
                },
            },
        },
        "one_line_summary": {"type": "string", "maxLength": 200},
        "plain_language": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "what_happened",
                "customer_impact",
                "why_it_happened",
                "what_we_recommend",
            ],
            "description": "The part of the comment a non-engineer reads. Write "
            "it for a support lead or product manager: complete sentences, no "
            "file paths, no class or method names, no error class names, no "
            "query syntax, no abbreviations they would have to look up. If the "
            "cause is unconfirmed, say so plainly here rather than implying a "
            "diagnosis.",
            "properties": {
                "what_happened": {
                    "type": "string",
                    "description": "2-3 sentences describing the observed "
                    "problem in business terms.",
                },
                "customer_impact": {
                    "type": "string",
                    "description": "Who was affected and how badly, including "
                    "'we could not determine this' when that is the truth.",
                },
                "why_it_happened": {
                    "type": "string",
                    "description": "The cause in plain English, with an explicit "
                    "hedge ('most likely', 'not yet confirmed') when the "
                    "confidence is not high.",
                },
                "what_we_recommend": {
                    "type": "string",
                    "description": "The proposed next step and who should own "
                    "it, in plain English.",
                },
            },
        },
        "root_cause": {
            "type": "object",
            "additionalProperties": False,
            "required": ["explanation", "component", "suggested_fix"],
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
                "suggested_fix": {
                    "type": "string",
                    "description": "REQUIRED. The resolution, stated so an "
                    "engineer could start on it: which function or config "
                    "changes, and what its behaviour becomes. Two to five "
                    "sentences. Do not restate the problem, and do not write a "
                    "vague instruction like 'investigate further' or 'add error "
                    "handling'. If you genuinely cannot propose a fix, write "
                    "'No fix proposed:' followed by the specific thing you would "
                    "need to know first and how someone would find it out.",
                },
                "fix_type": {
                    "type": "string",
                    "enum": [
                        "code_change",
                        "config_change",
                        "data_correction",
                        "infrastructure",
                        "no_change_needed",
                        "needs_more_investigation",
                    ],
                },
                "fix_verification": {
                    "type": "string",
                    "description": "How someone would prove the fix worked — the "
                    "specific query, metric or reproduction step that should "
                    "change, and what it should change to.",
                },
                "workaround": {
                    "type": "string",
                    "description": "Anything support can do for affected users "
                    "before the real fix ships, if such a thing exists.",
                },
                "alternative_hypotheses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Other causes consistent with the same "
                    "evidence, and what would distinguish them. Required "
                    "thinking whenever confidence is not high.",
                },
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


# The enum values are for the schema; nobody outside engineering should have to
# decode `narrowed_not_confirmed` in a ticket they have been asked to approve.
_VERDICT_LABEL = {
    "root_cause_identified": "cause identified",
    "narrowed_not_confirmed": "narrowed down, cause not yet confirmed",
    "not_reproducible_from_data": "could not reproduce this from the available data",
    "insufficient_information": "not enough information to investigate",
    "not_a_bug": "working as designed — not a defect",
}
_CONFIDENCE_LABEL = {
    "high": "high confidence",
    "medium": "moderate confidence — worth a human check",
    "low": "low confidence — treat as a starting point only",
}
_SIGNAL_LABEL = {
    "confirmed": "confirmed",
    "absent": "checked, nothing found",
    "not_checked": "not checked",
}
_FIX_TYPE_LABEL = {
    "code_change": "a code change",
    "config_change": "a configuration change",
    "data_correction": "a data correction",
    "infrastructure": "an infrastructure change",
    "no_change_needed": "no change needed",
    "needs_more_investigation": "more investigation needed before a fix is clear",
}

_NESTED_FIELDS = ("root_cause", "blast_radius", "plain_language", "signals_confirmed")
_LIST_FIELDS = ("evidence", "next_actions", "unverified")


def normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Coerce nested fields that arrived as strings back into structures.

    Observed in every recorded run: the model hands `blast_radius` and
    `root_cause` over as `"{'described': '...'}"` — a Python repr, not JSON.
    Rendering then called .get() on a string and produced a broken comment, so
    parse it back instead of losing the content.
    """
    out = dict(report)
    for key in _NESTED_FIELDS + _LIST_FIELDS:
        val = out.get(key)
        if not isinstance(val, str):
            continue
        parsed = _loads_loose(val)
        if isinstance(parsed, (dict, list)):
            out[key] = parsed
        elif key in _NESTED_FIELDS:
            # Unparseable: keep the text rather than dropping the analysis.
            out[key] = {"described": val, "explanation": val, "what_happened": val}
    return out


def _loads_loose(text: str) -> Any:
    import ast
    import json as _json

    for loader in (_json.loads, ast.literal_eval):
        try:
            return loader(text)
        except (ValueError, SyntaxError, MemoryError):
            continue
    return None


def validate_report(
    report: dict[str, Any], stats: dict[str, int] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic check on the claims the model made about its own certainty.

    The model is the least reliable judge of whether it verified something, and
    an over-confident triage comment is worse than a hedged one: it gets acted
    on. So the confidence field is capped by what the trace can support, and
    every downgrade is reported in the comment rather than applied silently.
    """
    r = dict(report)
    signals = r.get("signals_confirmed") or {}
    if not isinstance(signals, dict):
        signals = {}
    confirmed = {k for k, v in signals.items() if v == "confirmed"}
    notes: list[str] = []

    cited = [
        e for e in (r.get("evidence") or [])
        if isinstance(e, dict) and e.get("query_or_path")
    ]
    if not cited:
        notes.append(
            "No evidence item cites a query or file path, so nothing in this "
            "report is independently re-runnable."
        )

    # "high" is reserved for a log line, a data record and a code path agreeing.
    if r.get("confidence") == "high" and len(confirmed) < 2:
        r["confidence"] = "medium"
        # Don't name the destination: a later rule may lower it again, and a
        # note claiming "to medium" next to a "low" verdict reads as a bug.
        notes.append(
            f"Confidence downgraded: only {len(confirmed)} of the three signals "
            "(logs, code, data) was confirmed, and high is reserved for at "
            "least two."
        )

    if r.get("verdict") == "root_cause_identified" and "code" not in confirmed:
        notes.append(
            "Verdict claims an identified root cause, but the code path was not "
            "confirmed — treat the explanation as a leading hypothesis."
        )

    # Hearsay check. A report whose only evidence is Jira is a paraphrase of
    # what someone already wrote on the ticket, not an independent diagnosis.
    sources = {
        e.get("source") for e in (r.get("evidence") or []) if isinstance(e, dict)
    }
    first_party = sources & {"new_relic", "databricks", "code"}
    if not first_party and sources:
        if r.get("verdict") == "root_cause_identified":
            r["verdict"] = "narrowed_not_confirmed"
        r["confidence"] = "low"
        notes.append(
            "Every piece of evidence in this report came from Jira — existing "
            "ticket comments or related tickets — with nothing independently "
            "confirmed in New Relic or the code. This is a summary of what "
            "people already said, so the verdict has been reduced accordingly."
        )
    elif "new_relic" not in sources:
        notes.append(
            "No New Relic evidence was cited, so nothing here is corroborated "
            "by production telemetry."
        )

    if stats:
        empty, total = stats.get("empty_results", 0), stats.get("data_queries", 0)
        if total and empty == total:
            if r.get("confidence") == "high":
                r["confidence"] = "low"
            notes.append(
                f"Every one of the {total} data queries in this run returned no "
                "rows. A conclusion drawn from silence may just mean the data "
                "was queried in the wrong place."
            )
        elif total and empty / total > 0.6:
            notes.append(
                f"{empty} of {total} data queries returned no rows; the "
                "observability side of this diagnosis is thin."
            )

        nr_q, nr_hit = stats.get("nr_queries", 0), stats.get("nr_with_rows", 0)
        if nr_q == 0:
            if r.get("confidence") == "high":
                r["confidence"] = "low"
            notes.append(
                "New Relic was never queried in this run. The diagnosis rests "
                "entirely on the ticket text and the source code."
            )
        elif nr_hit == 0:
            if r.get("confidence") == "high":
                r["confidence"] = "low"
            notes.append(
                f"All {nr_q} New Relic queries came back empty, so no production "
                "telemetry corroborates this diagnosis."
            )

        log_q, log_hit = stats.get("log_queries", 0), stats.get("log_with_rows", 0)
        if log_q and not log_hit and signals.get("logs") == "absent":
            notes.append(
                f"{log_q} log queries were run and none returned a single line. "
                "That usually means logs are not reaching this account, or are "
                "not filterable by the attribute used — not that the system was "
                "quiet. `logs: absent` overstates what was established here."
            )
    return r, notes


def render_markdown(
    report: dict[str, Any],
    trace_md: str,
    run_id: str,
    caveats: list[str] | None = None,
) -> str:
    """Report -> the Jira comment body.

    Ordered for the widest reader first: a support lead or PM should get the
    whole picture from the summary section without scrolling into anything
    containing a file path, and an engineer picks up from "Technical detail".
    """
    r = normalize_report(report)
    rc = r.get("root_cause", {}) or {}
    loc = rc.get("code_location") or {}
    br = r.get("blast_radius", {}) or {}
    pl = r.get("plain_language", {}) or {}
    sig = r.get("signals_confirmed", {}) or {}

    lines = [
        f"*Automated triage* — {_VERDICT_LABEL.get(r['verdict'], r['verdict'])} "
        f"({_CONFIDENCE_LABEL.get(r['confidence'], r['confidence'])})",
        "",
        f"**{r['one_line_summary']}**",
        "",
    ]

    # ---- plain-language section, for readers who are not engineers ----------
    if pl:
        lines += ["### Summary"]
        for label, key in (
            ("What happened", "what_happened"),
            ("Who this affected", "customer_impact"),
            ("Why it happened", "why_it_happened"),
            ("What we recommend", "what_we_recommend"),
        ):
            if pl.get(key):
                lines += [f"**{label}.** {pl[key]}", ""]

    # ---- the resolution, always rendered ----------------------------------
    # This is the section people open the ticket for. Previously it was emitted
    # only when the optional `suggested_fix` happened to be filled in, so most
    # comments carried a diagnosis and no answer.
    lines += ["### Recommended fix"]
    fix = rc.get("suggested_fix") or (
        "_No fix was proposed for this ticket._ Treat the root cause above as a "
        "starting point and see the next actions below."
    )
    if rc.get("fix_type"):
        lines.append(f"_Type of change:_ {_FIX_TYPE_LABEL.get(rc['fix_type'], rc['fix_type'])}")
        lines.append("")
    lines += [fix, ""]
    if loc:
        where = loc.get("file", "?") + (f":{loc['line']}" if loc.get("line") else "")
        lines += [f"**Where to make it.** `{where}`"
                  + (f" — {loc['why']}" if loc.get("why") else ""), ""]
    if rc.get("fix_verification"):
        lines += [f"**How to confirm it worked.** {rc['fix_verification']}", ""]
    if rc.get("workaround"):
        lines += [f"**Interim workaround.** {rc['workaround']}", ""]

    if caveats:
        lines += ["### Read this before acting on it"]
        lines += [f"- {c}" for c in caveats]
        lines.append("")

    # ---- technical detail --------------------------------------------------
    lines += ["### Technical detail", ""]
    if sig:
        lines.append(
            "Signals checked — "
            + " · ".join(
                f"{k}: {_SIGNAL_LABEL.get(v, v)}" for k, v in sig.items()
            )
            + "\n"
        )
    lines += ["**Root cause**", rc.get("explanation", "_not determined_")]
    if rc.get("component"):
        lines.append(f"\nComponent: `{rc['component']}`")
    if loc and loc.get("symbol"):
        lines.append(f"Symbol: `{loc['symbol']}`")
    if rc.get("alternative_hypotheses"):
        lines += ["", "**Other explanations still consistent with the evidence**"]
        lines += [f"- {h}" for h in rc["alternative_hypotheses"]]

    lines += ["", "### Evidence"]
    for e in r.get("evidence", []):
        # Kept on one line: a wrapped continuation becomes a stray paragraph
        # once the markdown is converted to Jira's ADF.
        q = f" — source: `{e['query_or_path']}`" if e.get("query_or_path") else ""
        lines.append(f"- **[{e['source']}]** {e['claim']} — {e['detail']}{q}")

    lines += ["", "### How widespread it is", br.get("described", "_unknown_")]
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
