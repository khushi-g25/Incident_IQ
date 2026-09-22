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
        return {
            "data_queries": len(rows),
            "empty_results": len(empty),
            "errors": len(self.errors),
        }

    def evidence_markdown(self) -> str:
        """Appendix for the Jira comment: what the agent actually looked at."""
        entries = self.evidence_entries
        if not entries:
            return "_No tools were called._"
        lines = ["| # | tool | what | link |", "|---|---|---|---|"]
        for i, e in enumerate(entries, start=1):
            what = e.result_summary or e.error or ""
            link = f"[query]({e.evidence_link})" if e.evidence_link else ""
            lines.append(f"| {i} | `{e.tool}` | {what} | {link} |")
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
