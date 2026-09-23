"""Run trace.

An agent whose conclusions you cannot audit is a liability in an incident.
Every tool call, its arguments, its result size and a deep link to the
underlying query get appended here. The trace is written to disk per run and
the evidence links are embedded in the Jira comment, so any engineer can
re-execute the exact query the agent used.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# A New Relic deep link carries the query base64-encoded and runs to ~750
# characters — three of them cost more than the entire summary. Rationed hard,
# because the query text beside them is what makes a claim checkable.
MAX_EVIDENCE_LINKS = 2


def _cell(text: str) -> str:
    """Make a value safe to sit in a markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


@dataclass
class TraceEntry:
    seq: int
    tool: str
    args: dict[str, Any]
    result_summary: str | None = None
    evidence_link: str | None = None
    error: str | None = None
    elapsed_ms: int = 0
    at: float = field(default_factory=time.time)

    @property
    def target(self) -> str:
        """The thing this call actually interrogated, for the evidence table.

        Capped: a wide attribute-probe query runs to several hundred characters
        and would swamp the table. The untruncated query is in the permalink and
        in this trace file.
        """
        a = self.args or {}
        if nrql := a.get("nrql"):
            text = str(nrql)
            if len(text) > 150:
                text = text[:147] + "..."
            return f"`{text}`"
        if a.get("path"):
            svc = a.get("service", "")
            return f"`{svc}:{a['path']}`" if svc else f"`{a['path']}`"
        for key in ("jql", "query", "pattern", "file_path", "sql", "trace_id"):
            if a.get(key):
                return f"`{a[key]}`"
        if a.get("event_type"):
            return f"`{a['event_type']}`"
        if a.get("service"):
            return f"`{a['service']}`"
        return ""


@dataclass
class RunTrace:
    ticket: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    entries: list[TraceEntry] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    cost_usd: float = 0.0
    turns: int = 0
    _last: float = field(default_factory=time.time)

    def add(
        self,
        tool: str,
        args: dict[str, Any],
        result_summary: str | None = None,
        evidence_link: str | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        self.entries.append(
            TraceEntry(
                seq=len(self.entries) + 1,
                tool=tool,
                args={k: str(v)[:1000] for k, v in args.items()},
                result_summary=result_summary,
                evidence_link=evidence_link,
                error=error,
                elapsed_ms=int((now - self._last) * 1000),
            )
        )
        self._last = now

    @property
    def errors(self) -> list[TraceEntry]:
        return [e for e in self.entries if e.error]

    @property
    def evidence_entries(self) -> list[TraceEntry]:
        """Calls that actually produced or failed to produce evidence.

        Pre-flight rows exist to enforce the budget and to record denials; they
        cite nothing, so publishing them in the ticket just buries the real
        queries an engineer would want to re-run."""
        return [
            e for e in self.entries
            if e.result_summary != "(pre-flight ok)" or e.error
        ]

    @property
    def query_stats(self) -> dict[str, int]:
        """Signal on how much of the run actually returned data. Used to keep a
        confident verdict from resting on a pile of empty result sets."""
        rows = [e for e in self.entries if (e.result_summary or "").endswith("rows")]
        empty = [e for e in rows if (e.result_summary or "").startswith("0 ")]
        nr = [e for e in rows if e.tool == "nr_query"]
        nr_hits = [e for e in nr if not (e.result_summary or "").startswith("0 ")]
        logs = [e for e in nr if "FROM Log" in str((e.args or {}).get("nrql", ""))]
        log_hits = [e for e in logs if not (e.result_summary or "").startswith("0 ")]
        return {
            "data_queries": len(rows),
            "empty_results": len(empty),
            "errors": len(self.errors),
            # Did the agent actually get anything out of New Relic, and did it
            # ever manage to read a log line? A verdict reached without either
            # is a code-reading exercise, not a triage.
            "nr_queries": len(nr),
            "nr_with_rows": len(nr_hits),
            "log_queries": len(logs),
            "log_with_rows": len(log_hits),
        }

    def evidence_markdown(self, max_rows: int = 10, links: bool = True) -> str:
        """Appendix for the Jira comment: what the agent actually looked at.

        The query text itself goes in the table, so a reader can re-run a claim
        by hand. Both the row count and the links are capped, because Jira
        rejects a comment over ~32k characters and a sixty-call run with a deep
        link per row blows that on the appendix alone. The full, uncapped list
        is always in the run trace on disk.
        """
        entries = self.evidence_entries
        if not entries:
            return "_No tools were called._"

        shown, omitted = entries, 0
        if len(entries) > max_rows:
            # Keep the calls that carry information: failures first, then calls
            # that returned data, then the empty ones.
            def rank(i: int) -> tuple[int, int]:
                e = entries[i]
                summary = e.result_summary or ""
                if e.error:
                    return (0, i)
                if summary.endswith("rows") and not summary.startswith("0 "):
                    return (1, i)
                return (2, i)

            keep = sorted(sorted(range(len(entries)), key=rank)[:max_rows])
            shown = [entries[i] for i in keep]
            omitted = len(entries) - len(shown)

        header = "| # | tool | query or target | result | open |"
        divider = "|---|---|---|---|---|"
        if not links:
            header, divider = header.replace(" open |", ""), divider[:-4]

        lines = [header, divider]
        linked = 0
        for i, e in enumerate(shown, start=1):
            what = e.result_summary or e.error or ""
            row = f"| {i} | `{e.tool}` | {_cell(e.target)} | {_cell(what)} |"
            if links:
                # A New Relic deep link carries the whole query base64-encoded
                # and runs to ~750 characters; forty of them overflow Jira's
                # comment limit on their own. Only queries that returned
                # something get one — nobody needs to open an empty result, and
                # the query text beside it is copy-pasteable regardless.
                worth_opening = (
                    e.evidence_link
                    and what.endswith("rows")
                    and not what.startswith("0 ")
                    and linked < MAX_EVIDENCE_LINKS
                )
                if worth_opening:
                    row += f" [open]({e.evidence_link}) |"
                    linked += 1
                else:
                    row += "  |"
            lines.append(row)

        if omitted:
            lines += [
                "",
                f"_{omitted} further call(s) omitted — mostly empty results. "
                f"The complete list is in run `{self.run_id}`._",
            ]
        return "\n".join(lines)

    def logs_checked_markdown(self) -> str:
        """Spell out which logs were actually examined.

        "No errors in the logs" means nothing without knowing which logs were
        read, over what window, and whether the search returned anything at all.
        """
        log_calls = [
            e for e in self.entries
            if e.tool == "nr_query" and "FROM Log" in str((e.args or {}).get("nrql", ""))
        ]
        if not log_calls:
            return (
                "\n### Logs checked\n"
                "_No log search was run, so nothing in this report is based on "
                "log lines._"
            )

        lines = ["\n### Logs checked", "", "| # | filter | lines found |", "|---|---|---|"]
        found_any = any(
            (e.result_summary or "").endswith("rows")
            and not (e.result_summary or "").startswith("0 ")
            for e in log_calls
        )
        shown = log_calls[:6]
        for i, e in enumerate(shown, start=1):
            nrql = str((e.args or {}).get("nrql", ""))
            where = nrql.split("WHERE", 1)[1] if "WHERE" in nrql else "(no filter)"
            for clause in ("SINCE", "ORDER BY", "LIMIT", "FACET"):
                where = where.split(clause, 1)[0]
            summary = e.result_summary or e.error or ""
            lines.append(f"| {i} | {_cell(where.strip()[:110])} | {_cell(summary)} |")
        if len(log_calls) > len(shown):
            lines.append(f"| … | _{len(log_calls) - len(shown)} more log searches_ | |")

        if not found_any:
            lines += [
                "",
                "**Every log search came back empty.** That is consistent with "
                "logs not reaching this New Relic account, or not being "
                "filterable by the attribute used — it is not by itself "
                "evidence that nothing went wrong.",
            ]
        return "\n".join(lines)

    def save(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{self.ticket}-{self.run_id}.json"
        path.write_text(
            json.dumps(
                {
                    "ticket": self.ticket,
                    "run_id": self.run_id,
                    "started": self.started,
                    "duration_s": round(time.time() - self.started, 1),
                    "cost_usd": self.cost_usd,
                    "turns": self.turns,
                    "entries": [asdict(e) for e in self.entries],
                },
                indent=2,
            )
        )
        return path
