"""These are the tests that matter.

The agent's reasoning you evaluate empirically against resolved tickets. The
guardrails you unit-test, because a bypass here means a model-authored string
reaching your production warehouse.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from triage.clients.databricks import DatabricksClient, SqlRejected
from triage.clients.jira import _to_adf
from triage.clients.newrelic import NewRelicClient, NrqlRejected
from triage.config import DatabricksConfig, NewRelicConfig
from triage.extract import extract_brief
from triage.guardrails import build_hooks
from triage.redact import Redactor
from triage.schema import (
    REPORT_SCHEMA,
    normalize_report,
    render_markdown,
    validate_report,
)
from triage.trace import RunTrace

DB_CFG = DatabricksConfig(
    host="https://example.cloud.databricks.com", token="x", warehouse_id="w"
)
NR_CFG = NewRelicConfig(api_key="k", account_id=1)


@pytest.fixture
def db() -> DatabricksClient:
    return DatabricksClient.__new__(DatabricksClient).__class__(DB_CFG)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE main.orders.orders",
        "SELECT 1; DELETE FROM main.orders.orders",
        "UPDATE main.orders.orders SET status='synced'",
        "MERGE INTO main.orders.orders USING x ON 1=1",
        "select * from t; select * from u",
        "USE CATALOG hive_metastore",
        "COPY INTO main.x FROM 'abfss://...'",
    ],
)
def test_sql_writes_are_rejected(db: DatabricksClient, sql: str) -> None:
    with pytest.raises(SqlRejected):
        db.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM main.orders.orders WHERE order_id = :order_id",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SHOW TABLES IN main.orders",
        "DESCRIBE main.orders.orders",
    ],
)
def test_sql_reads_pass(db: DatabricksClient, sql: str) -> None:
    assert db.validate(sql)


def test_nrql_window_and_limit_are_injected() -> None:
    nr = NewRelicClient(NR_CFG)
    out = nr.validate("SELECT * FROM Log WHERE message LIKE '%boom%'")
    assert "SINCE" in out and "LIMIT" in out


def test_nrql_mutation_rejected() -> None:
    nr = NewRelicClient(NR_CFG)
    with pytest.raises(NrqlRejected):
        nr.validate("DELETE FROM Log SINCE 1 day ago")


def test_timeseries_keeps_no_limit() -> None:
    nr = NewRelicClient(NR_CFG)
    out = nr.validate("SELECT count(*) FROM Log TIMESERIES 5 minutes SINCE 1 hour ago")
    assert "LIMIT" not in out


# ------------------------------------------------- NRQL accuracy regressions
#
# Each of these reproduces a failure observed in a recorded production run.


@pytest.mark.parametrize(
    "nrql",
    [
        # 'create'/'update'/'delete' inside a transaction name is a read.
        "SELECT count(*) FROM Transaction WHERE name = "
        "'Controller/api/pos/batch_redemptions/create' SINCE 1 day ago",
        "SELECT count(*) FROM Transaction WHERE name LIKE '%/orders/update' "
        "SINCE 1 day ago",
        "SELECT count(*) FROM Log WHERE message = 'row insert failed' SINCE 1 day ago",
        "SELECT count(*) FROM Transaction FACET `delete.count` SINCE 1 day ago",
    ],
)
def test_endpoint_names_are_not_mistaken_for_mutations(nrql: str) -> None:
    assert NewRelicClient(NR_CFG).validate(nrql)


def test_real_mutation_still_rejected_outside_literals() -> None:
    nr = NewRelicClient(NR_CFG)
    for bad in ("DROP TABLE Log SINCE 1 day ago", "SELECT 1 FROM Log; DELETE FROM Log"):
        with pytest.raises(NrqlRejected):
            nr.validate(bad)


def test_bare_attribute_beside_aggregate_is_moved_into_facet() -> None:
    """NRQL rejects this outright; the intent is always 'group by'."""
    nr = NewRelicClient(NR_CFG)
    q, notes = nr.prepare(
        "FROM TransactionError SELECT count(*), error.class, error.message "
        "WHERE appName = 'Punchh' SINCE 1 day ago LIMIT MAX"
    )
    assert "SELECT count(*)" in q
    assert "error.class" not in q.split("FACET")[0]
    assert "FACET" in q and "error.class" in q and "error.message" in q
    # FACET must precede the window/limit clauses to stay valid NRQL.
    assert q.index("FACET") < q.index("SINCE") < q.index("LIMIT")
    assert notes


def test_bare_attribute_already_in_facet_is_just_dropped() -> None:
    nr = NewRelicClient(NR_CFG)
    q, _ = nr.prepare(
        "SELECT count(*), message FROM Log SINCE 1 day ago FACET message LIMIT 20"
    )
    assert q.count("message") == 1


def test_select_star_and_plain_attribute_selects_are_untouched() -> None:
    nr = NewRelicClient(NR_CFG)
    for q in (
        "SELECT * FROM Log SINCE 1 day ago",
        "SELECT timestamp, message FROM Log SINCE 1 day ago",
    ):
        assert "FACET" not in nr.validate(q)


def test_rewrite_does_not_edit_the_values_being_searched_for() -> None:
    """Tidying whitespace must not reach inside a string literal: '%two  spaces%'
    and '%two spaces%' are different queries."""
    q, _ = NewRelicClient(NR_CFG).prepare(
        "SELECT count(*), level FROM Log WHERE message LIKE '%two  spaces%' "
        "SINCE 1 day ago"
    )
    assert "'%two  spaces%'" in q
    assert "FACET `level`" in q


def test_backslash_escaped_quote_does_not_break_literal_detection() -> None:
    q, _ = NewRelicClient(NR_CFG).prepare(
        r"SELECT count(*) FROM Log WHERE appName LIKE '%O\'Brien%' "
        "AND message = 'create failed' SINCE 1 day ago"
    )
    assert r"'%O\'Brien%'" in q and "'create failed'" in q


def test_naked_absolute_window_warns_about_timezone() -> None:
    _, notes = NewRelicClient(NR_CFG).prepare(
        "SELECT count(*) FROM Log SINCE '2026-09-17 00:00:00' "
        "UNTIL '2026-09-17 23:59:59'"
    )
    assert any("offset" in n for n in notes)


def test_show_event_types_is_allowed_and_not_limited() -> None:
    out = NewRelicClient(NR_CFG).validate("SHOW EVENT TYPES SINCE 1 day ago")
    assert "LIMIT" not in out


# ---------------------------------------------------------------- redaction


def test_email_is_pseudonymised_stably() -> None:
    r = Redactor()
    a = r.scrub("contact jane.doe@example.com about it")
    b = r.scrub("jane.doe@example.com again")
    assert "jane.doe@example.com" not in a
    assert "<EMAIL_1>" in a and "<EMAIL_1>" in b


def test_card_number_redacted_but_order_number_kept() -> None:
    r = Redactor()
    out = r.scrub("card 4111111111111111 order 1234567890123456789")
    assert "4111111111111111" not in out       # passes Luhn -> redacted
    assert "1234567890123456789" in out        # fails Luhn -> preserved


def test_bearer_token_value_dropped_entirely() -> None:
    r = Redactor()
    out = r.scrub("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out


def test_unscrub_round_trips_for_the_human_report() -> None:
    r = Redactor()
    scrubbed = r.scrub("user a@b.com failed")
    assert r.unscrub(scrubbed) == "user a@b.com failed"


# ---------------------------------------------------------------- extraction

TICKET = """
Order ORD-99XK2LM4 failed to sync for store 4821 at 2026-09-14T11:32:07Z.
POS terminal PAR-1204 showed an error. Log excerpt:

  at com.par.orders.SyncJob.push(SyncJob.java:214)
  Caused by: LoyaltyTimeoutException: upstream did not respond
  status code 504

Customer jane@example.com is asking. Environment: production.
"""

PATTERNS = {
    "order_id": r"\border[_\s-]?(?:id|number|no)?\W{0,3}([A-Z0-9]{8,20})\b",
    "store_id": r"\b(?:store|site)[_\s-]?(?:id|code)?\W{0,3}(\d{3,6})\b",
    "terminal_id": r"\bterminal\W{0,3}([A-Z]{2,4}-\d{2,5})\b",
}


def test_extraction_pulls_the_things_you_need_to_query_on() -> None:
    b = extract_brief("EPS-1", "sync failure", TICKET, PATTERNS)
    assert "4821" in b.entities["store_id"]
    assert "PAR-1204" in b.entities["terminal_id"]
    assert "LoyaltyTimeoutException" in b.exception_classes
    assert "504" in b.http_statuses
    assert b.environment == "production"
    assert any(f.file == "SyncJob.java" and f.line == 214 for f in b.stack_frames)
    assert b.is_triageable


def test_time_window_is_padded_around_ticket_timestamps() -> None:
    b = extract_brief("EPS-1", "s", TICKET, PATTERNS, pad_minutes=30)
    window = b.nrql_window()
    assert "SINCE" in window and "UNTIL" in window
    assert "11:02" in window and "12:02" in window


def test_empty_ticket_is_gated_out() -> None:
    b = extract_brief("EPS-2", "it broke", "Please fix, thanks.", PATTERNS)
    assert not b.is_triageable


# ---------------------------------------------------------------- reporting

BASE_REPORT = {
    "verdict": "root_cause_identified",
    "confidence": "high",
    "signals_confirmed": {"logs": "confirmed", "code": "confirmed", "data": "confirmed"},
    "one_line_summary": "x",
    "plain_language": {
        "what_happened": "a",
        "customer_impact": "b",
        "why_it_happened": "c",
        "what_we_recommend": "d",
    },
    "root_cause": {"explanation": "e", "component": "f"},
    "evidence": [{"source": "code", "claim": "c", "detail": "d", "query_or_path": "a.rb"}],
    "blast_radius": {"described": "g"},
    "unverified": [],
    "next_actions": [{"action": "a", "owner_hint": "o"}],
}


def test_stringified_nested_objects_are_recovered() -> None:
    """Every recorded run handed these over as Python reprs, not JSON."""
    r = normalize_report(
        {
            **BASE_REPORT,
            "blast_radius": "{'described': \"can't quantify\"}",
            "root_cause": '{"explanation": "boom", "component": "svc"}',
        }
    )
    assert r["blast_radius"]["described"] == "can't quantify"
    assert r["root_cause"]["explanation"] == "boom"


def test_unparseable_nested_string_keeps_its_content() -> None:
    r = normalize_report({**BASE_REPORT, "blast_radius": "about 40 orders"})
    assert r["blast_radius"]["described"] == "about 40 orders"


def test_high_confidence_needs_two_confirmed_signals() -> None:
    report = {
        **BASE_REPORT,
        "signals_confirmed": {
            "logs": "not_checked",
            "code": "confirmed",
            "data": "not_checked",
        },
    }
    out, notes = validate_report(report, {"data_queries": 0, "empty_results": 0})
    assert out["confidence"] == "medium"
    assert any("lowered" in n for n in notes)


def test_all_empty_queries_forces_confidence_down() -> None:
    out, notes = validate_report(
        dict(BASE_REPORT), {"data_queries": 6, "empty_results": 6, "errors": 0}
    )
    assert out["confidence"] == "low"
    assert any("no rows" in n for n in notes)


def test_well_supported_report_keeps_high_confidence() -> None:
    out, notes = validate_report(
        dict(BASE_REPORT), {"data_queries": 6, "empty_results": 1, "errors": 0}
    )
    assert out["confidence"] == "high"
    assert notes == []


def test_uncited_evidence_is_flagged() -> None:
    report = {**BASE_REPORT, "evidence": [{"source": "code", "claim": "c", "detail": "d"}]}
    _, notes = validate_report(report, None)
    assert any("re-runnable" in n for n in notes)


def test_comment_leads_with_plain_language_before_any_code() -> None:
    """A non-engineer must get the whole picture before hitting a file path."""
    md = render_markdown(
        {
            **BASE_REPORT,
            "root_cause": {
                "explanation": "e",
                "component": "f",
                "code_location": {"file": "app/models/order.rb", "line": 9, "why": "w"},
            },
        },
        "trace",
        "run1",
    )
    assert md.index("What happened") < md.index("Technical detail")
    assert md.index("What we recommend") < md.index("app/models/order.rb")
    # Raw enum values should not reach the reader.
    assert "root_cause_identified" not in md
    assert "cause identified" in md


def test_recommended_fix_is_always_rendered() -> None:
    """The resolution section used to appear only when the optional
    suggested_fix happened to be filled in, so most comments had no answer."""
    with_fix = render_markdown(
        {
            **BASE_REPORT,
            "root_cause": {
                "explanation": "e",
                "component": "c",
                "suggested_fix": "Hoist the call out of the loop.",
                "fix_type": "code_change",
                "fix_verification": "p95 drops under 300ms.",
                "workaround": "None.",
            },
        },
        "t",
        "r",
    )
    assert "### Recommended fix" in with_fix
    assert "Hoist the call out of the loop." in with_fix
    assert "a code change" in with_fix
    assert "p95 drops under 300ms." in with_fix
    assert "None." in with_fix

    # Even with nothing proposed, the section exists and says so.
    without = render_markdown(dict(BASE_REPORT), "t", "r")
    assert "### Recommended fix" in without
    assert "No fix was proposed" in without


def test_suggested_fix_is_required_by_the_schema() -> None:
    assert "suggested_fix" in REPORT_SCHEMA["properties"]["root_cause"]["required"]


def test_caveats_are_surfaced_in_the_comment() -> None:
    md = render_markdown(dict(BASE_REPORT), "trace", "run1", ["watch out"])
    assert "watch out" in md


# ------------------------------------------------------------- Jira ADF output
#
# ADF has no markdown parser. Anything render_markdown emits that _to_adf does
# not convert posts to the ticket as literal punctuation.


def _adf(md: str) -> tuple[dict, str]:
    doc = _to_adf(md)
    return doc, json.dumps(doc)


def test_headings_become_heading_nodes() -> None:
    doc, blob = _adf("### Recommended fix\n\nDo the thing.")
    assert doc["content"][0]["type"] == "heading"
    assert doc["content"][0]["attrs"]["level"] == 3
    assert "###" not in blob


def test_bold_italic_and_inline_code_become_marks() -> None:
    _, blob = _adf("**What happened.** Look at `order.rb:12` or *maybe* not.")
    assert '"strong"' in blob and '"code"' in blob and '"em"' in blob
    assert "**" not in blob and "`" not in blob


def test_markdown_links_become_link_marks() -> None:
    _, blob = _adf("See [query](https://one.newrelic.com/x?a=1) for detail.")
    assert '"link"' in blob
    assert "https://one.newrelic.com/x?a=1" in blob
    assert "](" not in blob


def test_pipe_tables_become_adf_tables() -> None:
    doc, blob = _adf(
        "| # | tool | what |\n|---|---|---|\n| 1 | `nr_query` | 102 rows |\n"
        "| 2 | `gh_file` | 2795 chars |"
    )
    table = doc["content"][0]
    assert table["type"] == "table"
    assert len(table["content"]) == 3                       # header + 2 rows
    assert table["content"][0]["content"][0]["type"] == "tableHeader"
    assert table["content"][1]["content"][0]["type"] == "tableCell"
    assert "|---|" not in blob


def test_bullet_and_ordered_lists_are_distinguished() -> None:
    doc, _ = _adf("- one\n- two")
    assert doc["content"][0]["type"] == "bulletList"
    assert len(doc["content"][0]["content"]) == 2
    doc2, _ = _adf("1. one\n2. two")
    assert doc2["content"][0]["type"] == "orderedList"


def test_fenced_code_and_rules_survive() -> None:
    doc, _ = _adf("---\n\n```sql\nSELECT 1\nFROM t\n```")
    types = [n["type"] for n in doc["content"]]
    assert "rule" in types and "codeBlock" in types
    code = next(n for n in doc["content"] if n["type"] == "codeBlock")
    assert code["content"][0]["text"] == "SELECT 1\nFROM t"
    assert code["attrs"]["language"] == "sql"


def test_markers_inside_code_spans_are_literal() -> None:
    _, blob = _adf("Use `a ** b` carefully.")
    assert '"strong"' not in blob
    assert "a ** b" in blob


def test_wrapped_prose_stays_one_paragraph() -> None:
    doc, _ = _adf("This sentence is\nwrapped across lines.\n\nThis is separate.")
    paras = [n for n in doc["content"] if n["type"] == "paragraph"]
    assert len(paras) == 2
    assert paras[0]["content"][0]["text"] == "This sentence is wrapped across lines."


def test_a_full_rendered_report_converts_without_leftover_markup() -> None:
    md = render_markdown(
        {
            **BASE_REPORT,
            "root_cause": {
                "explanation": "e",
                "component": "c",
                "suggested_fix": "Do it.",
                "code_location": {"file": "app/x.rb", "line": 2, "why": "w"},
            },
            "evidence": [
                {
                    "source": "new_relic",
                    "claim": "rose",
                    "detail": "d",
                    "query_or_path": "SELECT count(*)",
                }
            ],
        },
        "| # | tool | what | link |\n|---|---|---|---|\n| 1 | `nr_query` | 9 rows | |",
        "run1",
        ["a caveat"],
    )
    doc, blob = _adf(md)
    for leftover in ("###", "**", "|---|", "`"):
        assert leftover not in blob, f"{leftover!r} reached the ticket unconverted"
    assert any(n["type"] == "heading" for n in doc["content"])
    assert any(n["type"] == "table" for n in doc["content"])


# ---------------------------------------------------------------- trace/budget


def test_preflight_rows_do_not_pollute_the_evidence_table() -> None:
    t = RunTrace(ticket="SQ-1")
    t.add("Glob", {}, result_summary="(pre-flight ok)")
    t.add("nr_query", {"nrql": "SELECT 1"}, result_summary="3 rows", evidence_link="u")
    md = t.evidence_markdown()
    assert "pre-flight" not in md
    assert "nr_query" in md


def test_denied_calls_stay_visible_in_the_evidence_table() -> None:
    t = RunTrace(ticket="SQ-1")
    t.add("nr_query", {}, error="rejected")
    assert "rejected" in t.evidence_markdown()


def _hook(trace: RunTrace, roots: list[str], budget: int = 60):
    hooks = build_hooks(trace, roots, max_tool_calls=budget)
    return hooks["PreToolUse"][0].hooks[0]


def _call(hook, name: str, args: dict | None = None) -> dict:
    return asyncio.run(hook({"tool_name": name, "tool_input": args or {}}, None, None))


def _denied(res: dict) -> bool:
    return res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_mcp_tools_are_not_double_counted_against_the_budget() -> None:
    """They record themselves on return; tracing them here too halved the
    effective budget and filled the evidence table with empty rows."""
    t = RunTrace(ticket="SQ-1")
    hook = _hook(t, [])
    _call(hook, "mcp__triage__nr_query", {"nrql": "SELECT 1"})
    assert t.entries == []


def test_builtin_tools_are_still_counted() -> None:
    t = RunTrace(ticket="SQ-1")
    hook = _hook(t, ["/tmp"])
    _call(hook, "Read", {"file_path": "/tmp/x.rb"})
    assert len(t.entries) == 1


def test_tool_search_is_not_charged_to_the_investigation_budget() -> None:
    t = RunTrace(ticket="SQ-1")
    hook = _hook(t, [])
    _call(hook, "ToolSearch", {"query": "select:x"})
    assert t.entries == []


def test_mutating_tools_are_denied() -> None:
    hook = _hook(RunTrace(ticket="SQ-1"), [])
    assert _denied(_call(hook, "Bash", {"command": "ls"}))
    assert _denied(_call(hook, "Write", {"file_path": "/tmp/x"}))


def test_local_reads_denied_when_no_repo_is_cloned() -> None:
    """The playbook's repo_paths are authored on another machine."""
    res = _call(_hook(RunTrace(ticket="SQ-1"), []), "Glob", {"path": "/nope"})
    assert _denied(res)
    assert "gh_search_code" in res["hookSpecificOutput"]["permissionDecisionReason"]


def test_reads_outside_the_repo_roots_are_denied(tmp_path) -> None:
    hook = _hook(RunTrace(ticket="SQ-1"), [str(tmp_path)])
    assert _denied(_call(hook, "Read", {"file_path": "/etc/passwd"}))
    inside = tmp_path / "a.rb"
    inside.write_text("x")
    assert not _denied(_call(hook, "Read", {"file_path": str(inside)}))


def test_budget_stops_the_run_but_lets_the_report_through() -> None:
    t = RunTrace(ticket="SQ-1")
    for _ in range(3):
        t.add("nr_query", {}, result_summary="1 rows")
    hook = _hook(t, [], budget=3)
    assert _denied(_call(hook, "mcp__triage__nr_query", {"nrql": "SELECT 1"}))
    assert not _denied(_call(hook, "StructuredOutput", {}))


def test_query_stats_count_empty_results() -> None:
    t = RunTrace(ticket="SQ-1")
    t.add("nr_query", {}, result_summary="0 rows")
    t.add("nr_query", {}, result_summary="12 rows")
    t.add("nr_query", {}, error="boom")
    assert t.query_stats == {"data_queries": 2, "empty_results": 1, "errors": 1}
