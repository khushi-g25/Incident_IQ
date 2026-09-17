"""These are the tests that matter.

The agent's reasoning you evaluate empirically against resolved tickets. The
guardrails you unit-test, because a bypass here means a model-authored string
reaching your production warehouse.
"""

from __future__ import annotations

import pytest

from triage.clients.databricks import DatabricksClient, SqlRejected
from triage.clients.newrelic import NewRelicClient, NrqlRejected
from triage.config import DatabricksConfig, NewRelicConfig
from triage.extract import extract_brief
from triage.redact import Redactor

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
