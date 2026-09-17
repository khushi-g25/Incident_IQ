"""New Relic NerdGraph client.

There is no REST endpoint for logs — everything goes through NRQL over the
NerdGraph GraphQL API. The three query shapes that matter for triage:

  Log          -> raw log lines            SELECT * FROM Log WHERE ...
  TransactionError / ErrorTrace -> stack traces and error classes
  Span         -> distributed trace spans, joined on trace.id

Always scope with SINCE/UNTIL. NRQL caps a single result set at 5000 rows, and
unbounded windows are both slow and expensive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import NewRelicConfig

_NRQL_QUERY = """
query($accountId: Int!, $nrql: Nrql!) {
  actor {
    account(id: $accountId) {
      nrql(query: $nrql, timeout: 70) {
        results
        metadata { eventTypes facets messages }
      }
    }
  }
}
"""

# Anything that writes, deletes or creates NR config is not triage.
_FORBIDDEN = re.compile(
    r"\b(DELETE|DROP|CREATE|UPDATE|INSERT|ALTER|GRANT)\b", re.IGNORECASE
)
_HAS_WINDOW = re.compile(r"\bSINCE\b", re.IGNORECASE)


class NrqlRejected(ValueError):
    pass


@dataclass
class NrqlResult:
    query: str
    results: list[dict[str, Any]]
    event_types: list[str]
    messages: list[str]
    permalink: str

    def truncated(self, n: int) -> "NrqlResult":
        return NrqlResult(
            self.query, self.results[:n], self.event_types, self.messages, self.permalink
        )


class NewRelicClient:
    def __init__(self, cfg: NewRelicConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self._http = client or httpx.Client(
            headers={"API-Key": cfg.api_key, "Content-Type": "application/json"},
            timeout=90.0,
        )

    def validate(self, nrql: str) -> str:
        """Reject mutating or unbounded queries before they leave the process."""
        q = nrql.strip().rstrip(";")
        if _FORBIDDEN.search(q):
            raise NrqlRejected("NRQL contains a forbidden keyword; reads only.")
        if not q.upper().startswith(("SELECT", "FROM")):
            raise NrqlRejected("NRQL must start with SELECT.")
        if not _HAS_WINDOW.search(q):
            q = f"{q} {self.cfg.default_window}"
        if not re.search(r"\bLIMIT\b", q, re.IGNORECASE) and "TIMESERIES" not in q.upper():
            q = f"{q} LIMIT {self.cfg.max_rows}"
        return q

    def nrql(self, query: str) -> NrqlResult:
        q = self.validate(query)
        r = self._http.post(
            self.cfg.endpoint,
            json={
                "query": _NRQL_QUERY,
                "variables": {"accountId": self.cfg.account_id, "nrql": q},
            },
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(f"NerdGraph error: {payload['errors']}")
        node = payload["data"]["actor"]["account"]["nrql"]
        meta = node.get("metadata") or {}
        return NrqlResult(
            query=q,
            results=node.get("results") or [],
            event_types=meta.get("eventTypes") or [],
            messages=meta.get("messages") or [],
            permalink=self.permalink(q),
        )

    def permalink(self, nrql: str) -> str:
        """Human-clickable query link to paste into the Jira comment as evidence."""
        from urllib.parse import quote

        return (
            f"https://one.newrelic.com/data-exploration/query-builder"
            f"?account={self.cfg.account_id}&query={quote(nrql)}"
        )
