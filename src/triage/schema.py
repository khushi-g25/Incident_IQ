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
        "prevention",
        "checklist",
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
                "summary",
                "customer_impact",
                "why_it_happened",
                "what_we_recommend",
                "solution_summary",
            ],
            "description": "The part of the comment a non-engineer reads. Write "
            "it for a support lead or product manager: complete sentences, no "
            "file paths, no class or method names, no error class names, no "
            "query syntax, no abbreviations they would have to look up. If the "
            "cause is unconfirmed, say so plainly here rather than implying a "
            "diagnosis.",
            "properties": {
                "summary": {
                    "type": "string",
                    "maxLength": 700,
                    "description": "The whole finding in 4-5 short sentences, "
                    "as one plain paragraph. This is the only part many readers "
                    "will read, so it must stand alone: what broke, who it "
                    "affected, why, and what happens next. Simple words, no "
                    "jargon, no file paths, no bullet points.",
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
                "solution_summary": {
                    "type": "string",
                    "maxLength": 400,
                    "description": "One or two sentences naming the solution "
                    "being proposed, in plain words, as a closing line for the "
                    "comment. A reader who skipped everything else should "
                    "finish knowing what is going to be done. Say the change "
                    "and the effect: 'Pass the campaign's own settings into the "
                    "audience query so it stops falling back to defaults.' If "
                    "no fix is proposed, say what happens instead.",
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
        "prevention": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "is_code_bug",
                "category",
                "what_to_change",
                "how_to_detect_next_time",
                "runbook_entry",
            ],
            "description": "Source material for the prevention document written "
            "alongside every run. Answer it even when nothing was broken in the "
            "code — 'this was a misconfiguration, here is how to spot it' is "
            "exactly the kind of knowledge that gets lost.",
            "properties": {
                "is_code_bug": {
                    "type": "string",
                    "enum": ["yes", "no", "unclear"],
                    "description": "Was a defect in the source code responsible? "
                    "'no' covers configuration, data, infrastructure and "
                    "user-error causes.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "logic_error",
                        "missing_validation",
                        "error_handling",
                        "performance",
                        "configuration",
                        "data_quality",
                        "infrastructure",
                        "third_party",
                        "user_error",
                        "unknown",
                    ],
                },
                "what_to_change": {
                    "type": "string",
                    "description": "The durable change that removes this class "
                    "of failure, not just this instance of it.",
                },
                "how_to_detect_next_time": {
                    "type": "string",
                    "description": "The test, alert or dashboard that would have "
                    "caught this before a customer did. Be specific about the "
                    "condition and the threshold.",
                },
                "runbook_entry": {
                    "type": "string",
                    "description": "A short entry someone can paste into the "
                    "service runbook: the symptom as it presents, and the first "
                    "thing to check. Write it for whoever picks up the next "
                    "ticket that looks like this one.",
                },
                "related_risk": {
                    "type": "string",
                    "description": "Anywhere else the same pattern likely "
                    "exists, if you saw one while reading the code.",
                },
            },
        },
        "checklist": {
            "type": "array",
            "minItems": 3,
            "description": "What has been done and what still has to happen, as "
            "tickable items. Cover three things: what this triage established "
            "(status 'done'), the fix itself (usually 'todo'), and at least one "
            "'prevention' item. Prevention must include writing or updating a "
            "document — a runbook entry, a note in the service README, a "
            "postmortem — so the next person recognises this failure instead of "
            "rediscovering it. Keep each item one short line.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "status", "category"],
                "properties": {
                    "item": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["done", "todo", "not_applicable"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "investigation",
                            "fix",
                            "prevention",
                            "communication",
                        ],
                    },
                    "owner": {"type": "string"},
                },
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
# Jira Cloud rejects a comment over 32,767 characters with
# CONTENT_LIMIT_EXCEEDED, and posts nothing at all. The budget is set well
# below that because it is measured on markdown: ADF serialises to roughly
# 1.8x the source, and unscrubbing redaction placeholders back to real values
# grows it again. A 39-call run overflowed the limit on its appendix alone.
MAX_COMMENT_CHARS = 16_000

_CODE_BUG_LABEL = {
    "yes": "Yes — a defect in the source code",
    "no": "No — the code behaved as written",
    "unclear": "Not established",
}
_PREVENTION_CATEGORY = {
    "logic_error": "logic error",
    "missing_validation": "missing validation",
    "error_handling": "error handling",
    "performance": "performance",
    "configuration": "configuration",
    "data_quality": "data quality",
    "infrastructure": "infrastructure",
    "third_party": "third-party dependency",
    "user_error": "expected behaviour / user error",
    "unknown": "not classified",
}
_CATEGORY_LABEL = {
    "investigation": "Investigation",
    "fix": "Fix",
    "prevention": "Prevention",
    "communication": "Comms",
}
_FIX_TYPE_LABEL = {
    "code_change": "a code change",
    "config_change": "a configuration change",
    "data_correction": "a data correction",
    "infrastructure": "an infrastructure change",
    "no_change_needed": "no change needed",
    "needs_more_investigation": "more investigation needed before a fix is clear",
}

_NESTED_FIELDS = (
    "root_cause", "blast_radius", "plain_language", "signals_confirmed", "prevention",
)
_LIST_FIELDS = ("evidence", "next_actions", "unverified", "checklist")


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

    # A triage that fixes the instance and leaves nothing behind gets the same
    # ticket again in six weeks, so the prevention item is not optional.
    checklist = [c for c in (r.get("checklist") or []) if isinstance(c, dict)]
    if checklist and not any(c.get("category") == "prevention" for c in checklist):
        notes.append(
            "The checklist has no prevention item. Nothing here stops this "
            "recurring — at minimum, someone should write the failure mode down "
            "in a runbook or the service README."
        )

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
    logs_md: str = "",
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
    # The paragraph leads, because most readers stop after it. The labelled
    # lines below it answer the follow-up questions without re-reading.
    if pl:
        if pl.get("summary"):
            lines += ["### Summary", pl["summary"], ""]
        for label, key in (
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

    # ---- checklist ---------------------------------------------------------
    if r.get("checklist"):
        lines += ["### Checklist", ""]
        for c in r["checklist"]:
            if not isinstance(c, dict) or not c.get("item"):
                continue
            box = "[x]" if c.get("status") == "done" else "[ ]"
            tail = ""
            if c.get("status") == "not_applicable":
                tail = " _(not applicable)_"
            elif c.get("owner"):
                tail = f" — _{c['owner']}_"
            cat = c.get("category")
            label = f"**{_CATEGORY_LABEL[cat]}:** " if cat in _CATEGORY_LABEL else ""
            lines.append(f"- {box} {label}{c['item']}{tail}")
        lines.append("")

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

    # ---- prevention --------------------------------------------------------
    pv = r.get("prevention") or {}
    if pv:
        lines += ["", "### Preventing a repeat"]
        lines.append(
            f"**Was this a code bug?** {_CODE_BUG_LABEL.get(pv.get('is_code_bug'), 'Not established')}"
            + (
                f" ({_PREVENTION_CATEGORY[pv['category']]})"
                if pv.get("category") in _PREVENTION_CATEGORY
                else ""
            )
        )
        if pv.get("what_to_change"):
            lines += ["", f"**What to change.** {pv['what_to_change']}"]
        if pv.get("how_to_detect_next_time"):
            lines += ["", f"**How we would catch it next time.** {pv['how_to_detect_next_time']}"]
        if pv.get("related_risk"):
            lines += ["", f"**Where else this may exist.** {pv['related_risk']}"]
        lines.append("")

    # ---- the closing line: what is actually being proposed -----------------
    if pl.get("solution_summary"):
        lines += ["", "### In short", pl["solution_summary"], ""]

    body = "\n".join(lines)
    footer = (
        f"\n_run {run_id} · generated by incident_iq · verify before acting on it_"
    )
    # Logs first: it is short and says whether the telemetry side of the
    # diagnosis stands up. The query table is long and is the part that gets
    # cut when the comment has to shrink.
    appendix = "\n".join(
        [
            logs_md,
            "",
            "### Queries run",
            trace_md,
            "",
            "_If a link does not open, copy the query text into New Relic's "
            "query builder._",
        ]
    )
    return _fit(body, appendix, footer, run_id)


def _fit(body: str, appendix: str, footer: str, run_id: str) -> str:
    """Keep the comment under Jira's size limit, sacrificing the appendix first.

    Jira answers an over-long comment with CONTENT_LIMIT_EXCEEDED and posts
    nothing, so the findings must never be the thing that gets dropped. The
    appendix is reproducible from the run trace; the diagnosis is not.
    """
    if len(body) + len(appendix) + len(footer) <= MAX_COMMENT_CHARS:
        return body + appendix + footer

    trunc_note = (
        "\n\n_Evidence appendix truncated to fit Jira's comment limit. "
        f"The full list of queries is in run `{run_id}`._"
    )
    room = MAX_COMMENT_CHARS - len(body) - len(footer) - len(trunc_note)
    if room > 400:
        trimmed = appendix[:room].rsplit("\n", 1)[0]
        return body + trimmed + trunc_note + footer

    # The findings alone are at the limit: drop the appendix entirely, and only
    # then start cutting the report itself.
    note = (
        f"\n\n_Evidence appendix omitted to fit Jira's comment limit — see run "
        f"`{run_id}`._"
    )
    if len(body) + len(note) + len(footer) <= MAX_COMMENT_CHARS:
        return body + note + footer
    keep = MAX_COMMENT_CHARS - len(note) - len(footer)
    return body[:keep].rsplit("\n", 1)[0] + note + footer
