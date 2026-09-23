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
from dataclasses import dataclass, field
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
#
# Only ever matched against a *literal-stripped* copy of the query. Endpoint and
# transaction names legitimately contain these words -- 'Controller/api/pos/
# batch_redemptions/create' is a read, not a write -- and matching them raw
# rejected a large share of otherwise valid latency queries.
_FORBIDDEN = re.compile(
    r"\b(DELETE|DROP|CREATE|UPDATE|INSERT|ALTER|GRANT)\b", re.IGNORECASE
)
_HAS_WINDOW = re.compile(r"\bSINCE\b", re.IGNORECASE)

# Single-quoted string literals and backticked identifiers. Everything inside
# them is data, never syntax. NRQL escapes a quote as \' , so consume
# backslash pairs rather than ending the literal on them.
_LITERAL = re.compile(r"'(?:[^'\\]|\\.|'')*'|`[^`]*`")

# Top-level clause keywords; used to find where the SELECT / FACET list ends.
_CLAUSE = re.compile(
    r"\b(?:FROM|WHERE|FACET|SINCE|UNTIL|LIMIT|TIMESERIES|ORDER\s+BY|COMPARE\s+WITH"
    r"|WITH|EXTRAPOLATE|SLIDE\s+BY)\b",
    re.IGNORECASE,
)

_ALIAS = re.compile(r"\s+AS\s+(?:'[^']*'|\"[^\"]*\"|`[^`]*`|[\w.]+)\s*$", re.IGNORECASE)


def _blank_literals(q: str) -> str:
    """Replace literal contents with spaces, preserving offsets."""
    return _LITERAL.sub(lambda m: " " * len(m.group(0)), q)


def _squeeze_outside_literals(q: str) -> str:
    """Tidy the whitespace a rewrite leaves behind, without touching literals.

    A blanket `\\s{2,} -> ' '` would silently edit the values being searched
    for: `message LIKE '%two  spaces%'` is not the same query.
    """
    out, pos = [], 0
    for m in _LITERAL.finditer(q):
        out.append(re.sub(r"\s+", " ", q[pos:m.start()]))
        out.append(m.group(0))          # verbatim
        pos = m.end()
    out.append(re.sub(r"\s+", " ", q[pos:]))
    return "".join(out).strip()


def _split_top_level(text: str) -> list[str]:
    """Split a clause list on commas that are not nested in parens or literals."""
    parts, depth, buf = [], 0, []
    masked = _blank_literals(text)
    for i, ch in enumerate(text):
        m = masked[i]
        if m == "(":
            depth += 1
        elif m == ")":
            depth -= 1
        if m == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _clause(q: str, keyword: str) -> tuple[int, int] | None:
    """Span of the list belonging to `keyword`, or None if absent."""
    masked = _blank_literals(q)
    m = re.search(rf"\b{keyword}\b", masked, re.IGNORECASE)
    if not m:
        return None
    nxt = _CLAUSE.search(masked, m.end())
    return (m.end(), nxt.start() if nxt else len(q))


class NrqlRejected(ValueError):
    pass


@dataclass
class NrqlResult:
    query: str
    results: list[dict[str, Any]]
    event_types: list[str]
    messages: list[str]
    permalink: str
    notes: list[str] = field(default_factory=list)

    def truncated(self, n: int) -> "NrqlResult":
        return NrqlResult(
            self.query, self.results[:n], self.event_types, self.messages,
            self.permalink, self.notes,
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
        return self.prepare(nrql)[0]

    def prepare(self, nrql: str) -> tuple[str, list[str]]:
        """Validate, then repair the mistakes that are cheap to fix in code.

        Returns the query to run plus any notes to hand back to the caller, so
        the agent learns what was changed instead of silently reasoning over a
        query it did not write.
        """
        q = nrql.strip().rstrip(";")
        masked = _blank_literals(q)

        if _FORBIDDEN.search(masked):
            raise NrqlRejected(
                "NRQL contains a forbidden keyword outside a string literal; "
                "reads only."
            )
        # SHOW EVENT TYPES is a read, but takes neither a SELECT list nor a
        # LIMIT, so it skips the rest of the pipeline.
        if masked.upper().startswith("SHOW"):
            return q, []

        if not masked.upper().startswith(("SELECT", "FROM")):
            raise NrqlRejected("NRQL must start with SELECT, FROM or SHOW.")

        notes: list[str] = []
        q, repair_notes = self._repair_select(q)
        notes += repair_notes

        if not _HAS_WINDOW.search(_blank_literals(q)):
            q = f"{q} {self.cfg.default_window}"
            notes.append(f"No SINCE clause given; defaulted to {self.cfg.default_window}.")
        else:
            notes += self._timezone_notes(q)

        masked = _blank_literals(q)
        if not re.search(r"\bLIMIT\b", masked, re.IGNORECASE) and not re.search(
            r"\bTIMESERIES\b", masked, re.IGNORECASE
        ):
            q = f"{q} LIMIT {self.cfg.max_rows}"
        return q, notes

    @staticmethod
    def _repair_select(q: str) -> tuple[str, list[str]]:
        """NRQL forbids mixing aggregates with bare attributes in SELECT.

        `SELECT count(*), error.class FROM TransactionError FACET error.class`
        fails with "Value must be constant in its context" -- a mistake models
        make constantly because it is valid SQL. The intent is always "group by
        these", so move the bare attributes into FACET rather than failing.
        """
        span = _clause(q, "SELECT")
        if not span:
            return q, []
        start, end = span
        items = _split_top_level(q[start:end])
        if not items or any(i.strip() == "*" for i in items):
            return q, []

        aggregates, bare = [], []
        for item in items:
            body = _ALIAS.sub("", item).strip()
            if "(" in _blank_literals(body):
                aggregates.append(item)
            else:
                bare.append(body.strip("`"))
        if not aggregates or not bare:
            return q, []

        facet_span = _clause(q, "FACET")
        existing = (
            [f.strip("` ") for f in _split_top_level(q[facet_span[0]:facet_span[1]])]
            if facet_span
            else []
        )
        missing = [b for b in bare if b.lower() not in {e.lower() for e in existing}]

        rebuilt = q[:start] + " " + ", ".join(aggregates) + " " + q[end:]
        note = (
            f"Removed bare attribute(s) {', '.join(bare)} from SELECT: NRQL "
            "cannot return an aggregate and a raw attribute together."
        )
        if missing:
            facet_span = _clause(rebuilt, "FACET")
            if facet_span:
                rebuilt = (
                    rebuilt[: facet_span[1]].rstrip()
                    + ", "
                    + ", ".join(f"`{m}`" for m in missing)
                    + " "
                    + rebuilt[facet_span[1]:]
                )
            else:
                # FACET has to land before the window/limit clauses, not at the
                # end of the string.
                facet = f" FACET {', '.join(f'`{m}`' for m in missing)} "
                tail = re.search(
                    r"\b(?:SINCE|UNTIL|LIMIT|TIMESERIES|ORDER\s+BY|COMPARE\s+WITH)\b",
                    _blank_literals(rebuilt),
                    re.IGNORECASE,
                )
                rebuilt = (
                    rebuilt[: tail.start()] + facet + rebuilt[tail.start():]
                    if tail
                    else rebuilt.rstrip() + facet
                )
            note += f" Added {', '.join(missing)} to FACET instead."
        else:
            note += " They were already in FACET, so no data is lost."
        return _squeeze_outside_literals(rebuilt), [note]

    @staticmethod
    def _timezone_notes(q: str) -> list[str]:
        """Absolute SINCE/UNTIL without an offset resolves in the *account*
        timezone, which silently shifts every window the agent derives from a
        ticket. Warn rather than rewrite -- guessing the offset would be worse."""
        naked = re.findall(
            r"\b(?:SINCE|UNTIL)\s+'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)'",
            q,
            re.IGNORECASE,
        )
        if not naked:
            return []
        return [
            "Absolute time bound(s) "
            + ", ".join(f"'{n}'" for n in naked)
            + " have no UTC offset, so New Relic read them in the account "
            "timezone. Append ' +0000' (or 'Z') if the ticket times were UTC."
        ]

    def nrql(self, query: str) -> NrqlResult:
        q, notes = self.prepare(query)
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
            notes=notes,
        )

    def permalink(self, nrql: str) -> str:
        """Human-clickable query link to paste into the Jira comment as evidence.

        `platform[accountId]` is what actually sets the account context in New
        Relic One. Without it the query builder opens with no account selected
        and runs nothing, which reads as "the link is broken". The raw NRQL is
        published alongside this link in the evidence table, so a reader can
        always paste the query by hand if the deep link misbehaves.
        """
        from urllib.parse import quote

        q = quote(nrql, safe="")
        return (
            "https://one.newrelic.com/data-exploration/query-builder"
            f"?platform[accountId]={self.cfg.account_id}"
            f"&account={self.cfg.account_id}"
            f"&query={q}"
        )
