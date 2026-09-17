"""Deterministic pre-extraction.

Do not make the model do regex work. Pull identifiers, timestamps, error
signatures and stack frames out of the ticket with code, hand the model a clean
structured brief, and let it spend its turns on reasoning instead of parsing.

Everything here is cheap, testable, and gives you a hard gate: if no entities
and no error signature come out, the ticket is not machine-triageable and
should be bounced back for more detail rather than burning an agent run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
TRACE_ID_RE = re.compile(r"\b(?:trace[_.-]?id|traceId)\W{0,3}([0-9a-f]{16,32})\b", re.I)
ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
)
EPOCH_MS_RE = re.compile(r"\b1[0-9]{12}\b")
HTTP_STATUS_RE = re.compile(r"\b(?:status(?:\s*code)?|HTTP)\D{0,3}([45]\d{2})\b", re.I)

# Exception classes across the stacks you're likely to hit.
EXCEPTION_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]*(?:Exception|Error|Failure|Timeout|Fault))\b"
)
# Java / Python / Ruby / JS stack frame shapes.
FRAME_RE = re.compile(
    r"""(?:
        at\s+(?P<java>[\w.$]+)\((?P<jfile>[\w.]+):(?P<jline>\d+)\)
      | File\s+"(?P<pyfile>[^"]+)",\s+line\s+(?P<pyline>\d+)
      | (?P<rbfile>[\w./-]+\.rb):(?P<rbline>\d+):in
      | at\s+(?:[\w.<>\[\]]+\s+)?\(?(?P<jsfile>[\w./@-]+\.(?:js|ts|jsx|tsx)):(?P<jsline>\d+)
    )""",
    re.VERBOSE,
)


@dataclass
class StackFrame:
    file: str
    line: int
    symbol: str | None = None

    def as_str(self) -> str:
        sym = f" ({self.symbol})" if self.symbol else ""
        return f"{self.file}:{self.line}{sym}"


@dataclass
class TicketBrief:
    """Structured, model-ready summary of a ticket."""

    key: str
    summary: str
    entities: dict[str, list[str]] = field(default_factory=dict)
    timestamps: list[str] = field(default_factory=list)
    time_window: tuple[str, str] | None = None
    exception_classes: list[str] = field(default_factory=list)
    http_statuses: list[str] = field(default_factory=list)
    stack_frames: list[StackFrame] = field(default_factory=list)
    environment: str | None = None
    services: list[str] = field(default_factory=list)

    @property
    def is_triageable(self) -> bool:
        has_signal = bool(
            self.exception_classes or self.stack_frames or self.http_statuses
        )
        has_subject = any(v for v in self.entities.values())
        return has_signal or has_subject

    def nrql_window(self) -> str:
        """SINCE/UNTIL clause derived from ticket timestamps, padded either side."""
        if not self.time_window:
            return "SINCE 24 hours ago"
        start, end = self.time_window
        return f"SINCE '{start}' UNTIL '{end}'"

    def as_prompt_text(self) -> str:
        lines = [f"## Extracted brief for {self.key}"]
        for name, vals in self.entities.items():
            if vals:
                lines.append(f"- {name}: {', '.join(vals[:10])}")
        if self.environment:
            lines.append(f"- environment: {self.environment}")
        if self.services:
            lines.append(f"- candidate services: {', '.join(self.services)}")
        if self.exception_classes:
            lines.append(f"- exceptions: {', '.join(self.exception_classes)}")
        if self.http_statuses:
            lines.append(f"- http statuses: {', '.join(self.http_statuses)}")
        if self.stack_frames:
            lines.append("- stack frames (top first):")
            lines += [f"    {f.as_str()}" for f in self.stack_frames[:12]]
        lines.append(f"- suggested NRQL window: {self.nrql_window()}")
        return "\n".join(lines)


def _dedupe(seq: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for s in seq:
        seen.setdefault(s, None)
    return list(seen)


def _parse_ts(raw: str) -> datetime | None:
    cleaned = raw.replace(" ", "T").rstrip("Z")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return datetime.strptime(cleaned[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_brief(
    key: str,
    summary: str,
    text: str,
    entity_patterns: dict[str, str],
    services: dict[str, Any] | None = None,
    pad_minutes: int = 30,
) -> TicketBrief:
    """Build a TicketBrief from raw ticket text.

    `entity_patterns` comes from the playbook, so domain IDs (order numbers,
    loyalty member IDs, store codes) are declared as config, not hardcoded.
    """
    entities: dict[str, list[str]] = {}
    for name, pattern in entity_patterns.items():
        found = [m.group(0) if not m.groups() else m.group(1)
                 for m in re.finditer(pattern, text, re.IGNORECASE)]
        entities[name] = _dedupe(found)[:20]

    entities.setdefault("uuid", [])
    entities["uuid"] = _dedupe(entities["uuid"] + UUID_RE.findall(text))[:20]
    entities["trace_id"] = _dedupe(
        [m.group(1) for m in TRACE_ID_RE.finditer(text)]
    )[:20]

    raw_ts = _dedupe(ISO_TS_RE.findall(text))
    parsed = sorted(t for t in (_parse_ts(r) for r in raw_ts) if t)
    window = None
    if parsed:
        lo = parsed[0] - timedelta(minutes=pad_minutes)
        hi = parsed[-1] + timedelta(minutes=pad_minutes)
        window = (
            lo.strftime("%Y-%m-%d %H:%M:%S+0000"),
            hi.strftime("%Y-%m-%d %H:%M:%S+0000"),
        )

    frames: list[StackFrame] = []
    for m in FRAME_RE.finditer(text):
        g = m.groupdict()
        if g.get("jfile"):
            frames.append(StackFrame(g["jfile"], int(g["jline"]), g.get("java")))
        elif g.get("pyfile"):
            frames.append(StackFrame(g["pyfile"], int(g["pyline"])))
        elif g.get("rbfile"):
            frames.append(StackFrame(g["rbfile"], int(g["rbline"])))
        elif g.get("jsfile"):
            frames.append(StackFrame(g["jsfile"], int(g["jsline"])))

    env = None
    for candidate in ("production", "prod", "staging", "stage", "qa", "sandbox", "dev"):
        if re.search(rf"\b{candidate}\b", text, re.IGNORECASE):
            env = candidate
            break

    matched_services = []
    for svc, meta in (services or {}).items():
        needles = [svc] + list(meta.get("aliases") or [])
        if any(re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE) for n in needles):
            matched_services.append(svc)

    return TicketBrief(
        key=key,
        summary=summary,
        entities={k: v for k, v in entities.items() if v},
        timestamps=raw_ts[:20] + _dedupe(EPOCH_MS_RE.findall(text))[:10],
        time_window=window,
        exception_classes=_dedupe(EXCEPTION_RE.findall(text))[:10],
        http_statuses=_dedupe([m.group(1) for m in HTTP_STATUS_RE.finditer(text)]),
        stack_frames=frames[:25],
        environment=env,
        services=matched_services,
    )
