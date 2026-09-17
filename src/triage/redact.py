"""Redaction layer.

Logs and loyalty/order tables carry real customer data. Everything returned by a
tool passes through here before it enters the model's context, and the mapping
is kept in-process so the agent can still correlate ("EMAIL_1 appears in both
the log line and the order row") without ever seeing the actual value.

This is a reduction in exposure, not a guarantee. Pair it with column
allowlists in the playbook so sensitive columns are never SELECTed at all.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "PAN": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "PHONE": re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:\(?\d{3}\)?[ -]?)\d{3}[ -]?\d{4}(?!\d)"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # Group 1 is the keyword, so the value can be dropped without keeping it.
    "BEARER": re.compile(
        r"(?i)\b(bearer|api[-_]?key|apikey|token|password|passwd|secret)\b"
        r"\s*[:=]?\s+(?:\S+)"
    ),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}


def _luhn(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()][::-1]
    if not 13 <= len(ds) <= 19:
        return False
    total = 0
    for i, d in enumerate(ds):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class Redactor:
    """Stable, reversible-in-process pseudonymisation."""

    _map: dict[str, str] = field(default_factory=dict)
    _reverse: dict[str, str] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)

    def _placeholder(self, kind: str, value: str) -> str:
        if value in self._map:
            return self._map[value]
        self._counts[kind] = self._counts.get(kind, 0) + 1
        token = f"<{kind}_{self._counts[kind]}>"
        self._map[value] = token
        self._reverse[token] = value
        return token

    def scrub(self, text: str) -> str:
        if not text:
            return text
        out = text
        for kind, pattern in PATTERNS.items():
            def sub(m: re.Match[str], kind: str = kind) -> str:
                raw = m.group(0)
                if kind == "PAN" and not _luhn(raw):
                    return raw          # order numbers etc. are not card numbers
                if kind == "BEARER":
                    # Keep the keyword for context, discard the credential.
                    return f"{m.group(1)} <REDACTED>"
                return self._placeholder(kind, raw)

            out = pattern.sub(sub, out)
        return out

    def unscrub(self, text: str) -> str:
        """For the human-facing report only, where the reader is authorised."""
        out = text
        for token, value in self._reverse.items():
            out = out.replace(token, value)
        return out

    def fingerprint(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:12]

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._counts)
