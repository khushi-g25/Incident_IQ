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
            if len(text) > 180:
                text = text[:177] + "..."
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

    def evidence_markdown(self) -> str:
        """Appendix for the Jira comment: what the agent actually looked at.

        The query text itself goes in the table. Previously only the tool name,
        the row count and a deep link were published, so a reader who wanted to
        check a claim had nothing to copy into New Relic — and nothing at all if
        the deep link failed to open.
        """
        entries = self.evidence_entries
        if not entries:
            return "_No tools were called._"
        lines = ["| # | tool | query or target | result | open |", "|---|---|---|---|---|"]
        for i, e in enumerate(entries, start=1):
            what = e.result_summary or e.error or ""
            link = f"[open]({e.evidence_link})" if e.evidence_link else ""
            lines.append(
                f"| {i} | `{e.tool}` | {_cell(e.target)} | {_cell(what)} | {link} |"
            )
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
