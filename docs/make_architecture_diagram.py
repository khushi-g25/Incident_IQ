"""Generate the architecture diagram as SVG (and PNG via headless Chrome).

Kept as a script rather than a hand-written SVG so the layout can be adjusted
without re-deriving three hundred coordinates by hand.

    python3 docs/make_architecture_diagram.py

Writes docs/architecture.svg and, if Chrome is installed, docs/architecture.png
at 3200x1800 — sized for a 16:9 slide at 2x, so it stays sharp when projected.
"""

from __future__ import annotations

import html
import shutil
import subprocess
import tempfile
from pathlib import Path

W, H = 1600, 900
OUT = Path(__file__).resolve().parent

FONT = (
    "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "'Helvetica Neue', Arial, sans-serif"
)
MONO = "'SF Mono', SFMono-Regular, ui-monospace, Menlo, Consolas, monospace"

INK = "#151b21"
MUTED = "#5b6672"
ARROW = "#8b97a3"

# One fill/stroke pair per role, so the colour carries meaning: blue is
# deterministic code, amber is the model, red is a hard gate, green is output.
DET = ("#eef3f9", "#9cb6d1")
AGENT = ("#fff7e8", "#e2a531")
GUARD = ("#fdeeec", "#d4544f")
OUTP = ("#eaf7ef", "#39a065")
EXT = ("#f5f7f9", "#bcc6d0")

parts: list[str] = []


def esc(t: str) -> str:
    return html.escape(t, quote=False)


def box(
    x: float, y: float, w: float, h: float,
    fill: tuple[str, str], r: float = 10, dash: str = "", width: float = 1.6,
) -> None:
    f, s = fill
    d = f' stroke-dasharray="{dash}"' if dash else ""
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" ry="{r}" '
        f'fill="{f}" stroke="{s}" stroke-width="{width}"{d}/>'
    )


def text(
    x: float, y: float, s: str, size: float = 15, weight: int = 400,
    fill: str = INK, anchor: str = "start", mono: bool = False,
    opacity: float = 1.0,
) -> None:
    parts.append(
        f'<text x="{x}" y="{y}" font-family="{MONO if mono else FONT}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}" opacity="{opacity}">{esc(s)}</text>'
    )


def lines(x: float, y: float, rows: list[str], size: float = 13.5,
          lh: float = 19, fill: str = MUTED, mono: bool = False) -> None:
    for i, row in enumerate(rows):
        text(x, y + i * lh, row, size=size, fill=fill, mono=mono)


def arrow(x1: float, y1: float, x2: float, y2: float,
          dash: str = "", color: str = ARROW) -> None:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    parts.append(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
        f'stroke-width="2"{d} marker-end="url(#a)"/>'
    )


def chip(x: float, y: float, label: str, w: float = 0, size: float = 12.5) -> float:
    """Small pill used for the tool groups."""
    w = w or 9 + len(label) * 6.9
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="24" rx="12" ry="12" '
        f'fill="#ffffff" stroke="#e0c789" stroke-width="1.3"/>'
    )
    text(x + w / 2, y + 16.5, label, size=size, anchor="middle", fill="#6b5320")
    return w


# --------------------------------------------------------------------- canvas
parts.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
    f'viewBox="0 0 {W} {H}">'
)
parts.append(
    '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" '
    'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{ARROW}"/></marker></defs>'
)
parts.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

# ---------------------------------------------------------------------- title
text(40, 58, "Incident IQ — Architecture", size=31, weight=700)
text(
    40, 86,
    "Jira ticket in, evidence-backed root cause out. One model step; "
    "everything around it is deterministic.",
    size=15, fill=MUTED,
)
parts.append(
    f'<line x1="40" y1="104" x2="{W-40}" y2="104" stroke="#e3e8ed" '
    'stroke-width="1.5"/>'
)

BAND_Y, BAND_H = 148, 382          # the main pipeline band

# ---------------------------------------------------------- 1. entry points
box(40, BAND_Y, 180, BAND_H, EXT)
text(58, BAND_Y + 28, "ENTRY POINTS", size=11.5, weight=700, fill="#6a7481")
for i, (name, sub) in enumerate(
    [("CLI", "triage SQ-1234"),
     ("Approval UI", "port 8090"),
     ("Jira webhook", "port 8080")]
):
    y = BAND_Y + 58 + i * 100
    box(58, y, 144, 66, ("#ffffff", "#ccd5de"), r=8)
    text(70, y + 27, name, size=15, weight=600)
    text(70, y + 48, sub, size=12, fill=MUTED, mono=True)
arrow(228, BAND_Y + BAND_H / 2, 250, BAND_Y + BAND_H / 2)

# ------------------------------------------------------------- 2. intake
IX, IW = 258, 214
box(IX, BAND_Y, IW, BAND_H, DET)
text(IX + 18, BAND_Y + 28, "1 · INTAKE", size=11.5, weight=700, fill="#3f6188")
text(IX + 18, BAND_Y + 50, "deterministic — no model", size=11.5, fill=MUTED)
lines(IX + 18, BAND_Y + 82, [
    "fetch issue + comments",
    "pull .log / .txt attachments",
], size=13)
box(IX + 18, BAND_Y + 112, IW - 36, 80, ("#ffffff", "#ccd5de"), r=8)
text(IX + 30, BAND_Y + 136, "extract.py", size=13.5, weight=600, mono=True)
lines(IX + 30, BAND_Y + 156, [
    "ids · timestamps · exceptions",
    "stack frames · NRQL window",
], size=11.5, lh=16)
box(IX + 18, BAND_Y + 204, IW - 36, 54, ("#ffffff", "#ccd5de"), r=8)
text(IX + 30, BAND_Y + 227, "redact.py", size=13.5, weight=600, mono=True)
text(IX + 30, BAND_Y + 246, "PII → <EMAIL_1> placeholders", size=11.5, fill=MUTED)
text(IX + 18, BAND_Y + BAND_H - 14, "→ TicketBrief", size=12.5, weight=600,
     fill="#3f6188")
arrow(IX + IW + 8, BAND_Y + BAND_H / 2, IX + IW + 30, BAND_Y + BAND_H / 2)

# --------------------------------------------------------------- 3. gate
GX, GW = 500, 118
GY = BAND_Y + (BAND_H - 146) / 2
box(GX, GY, GW, 146, GUARD)
text(GX + GW / 2, GY + 28, "2 · GATE", size=11.5, weight=700,
     anchor="middle", fill="#a33c38")
text(GX + GW / 2, GY + 54, "machine-", size=13, anchor="middle")
text(GX + GW / 2, GY + 71, "usable", size=13, anchor="middle")
text(GX + GW / 2, GY + 88, "signal?", size=13, anchor="middle")
text(GX + GW / 2, GY + 120, "no → bounce", size=11.5, anchor="middle",
     fill="#a33c38", weight=600)
arrow(GX + GW + 8, BAND_Y + BAND_H / 2, GX + GW + 30, BAND_Y + BAND_H / 2)

# --------------------------------------------------------- 4. agent loop
AX, AW = 646, 456
box(AX, BAND_Y, AW, BAND_H, AGENT, width=2.2)
text(AX + 20, BAND_Y + 28, "3 · AGENT LOOP", size=11.5, weight=700,
     fill="#8a6414")
text(AX + 20, BAND_Y + 50, "Claude Agent SDK · the only non-deterministic step",
     size=11.5, fill=MUTED)

# inputs
box(AX + 20, BAND_Y + 66, 196, 74, ("#ffffff", "#e0c789"), r=8)
text(AX + 32, BAND_Y + 88, "prompts/system.md", size=12.5, weight=600, mono=True)
text(AX + 32, BAND_Y + 107, "the triage method", size=11.5, fill=MUTED)
text(AX + 32, BAND_Y + 128, "playbook.yaml — your estate", size=11.5, fill=MUTED)

box(AX + 232, BAND_Y + 66, 204, 74, ("#ffffff", "#e0c789"), r=8)
text(AX + 244, BAND_Y + 88, "REPORT_SCHEMA", size=12.5, weight=600, mono=True)
text(AX + 244, BAND_Y + 107, "forced structured output", size=11.5, fill=MUTED)
text(AX + 244, BAND_Y + 128, "verdict · fix · prevention", size=11.5, fill=MUTED)

# tools
text(AX + 20, BAND_Y + 166, "tools.py — in-process MCP · 13 read-only tools",
     size=12.5, weight=600)
groups = [
    ("discover", ["nr_apps", "nr_event_types", "nr_attributes"]),
    ("telemetry", ["nr_query", "nr_find_errors", "nr_logs", "nr_trace"]),
    ("code", ["gh_search_code", "gh_file", "gh_blame", "gh_file_history"]),
    ("history", ["jira_related_tickets"]),
]
ty = BAND_Y + 178
for label, items in groups:
    text(AX + 20, ty + 17, label, size=11.5, weight=600, fill="#8a6414")
    cx = AX + 92
    for it in items:
        cx += chip(cx, ty, it) + 6
    ty += 30

# guardrails
box(AX + 20, BAND_Y + BAND_H - 42, AW - 40, 22, GUARD, r=6)
text(AX + 30, BAND_Y + BAND_H - 26.5,
     "guardrails.py · PreToolUse: no writes · no path escape · 60-call budget",
     size=11.5, weight=600, fill="#a33c38")

arrow(AX + AW + 8, BAND_Y + BAND_H / 2, AX + AW + 30, BAND_Y + BAND_H / 2)

# ------------------------------------------------------------- 5. render
RX, RW = 1130, 202
box(RX, BAND_Y, RW, BAND_H, DET)
text(RX + 18, BAND_Y + 28, "4 · RENDER", size=11.5, weight=700, fill="#3f6188")
text(RX + 18, BAND_Y + 50, "deterministic", size=11.5, fill=MUTED)
for i, (name, sub) in enumerate([
    ("normalize_report", "repair the shape"),
    ("validate_report", "cap confidence"),
    ("render_markdown", "fit to 16k"),
    ("_to_adf", "Jira rich text"),
]):
    y = BAND_Y + 70 + i * 62
    box(RX + 18, y, RW - 36, 52, ("#ffffff", "#ccd5de"), r=8)
    text(RX + 30, y + 22, name, size=12.5, weight=600, mono=True)
    text(RX + 30, y + 40, sub, size=11.5, fill=MUTED)

# ------------------------------------------------- external systems (bottom)
EY = BAND_Y + BAND_H + 58
box(40, EY, 1060, 96, EXT, dash="5 4")
text(58, EY + 26, "READ-ONLY SOURCES", size=11.5, weight=700, fill="#6a7481")
text(200, EY + 26, "— no write methods exist in the client code",
     size=11.5, fill=MUTED)
for i, (name, sub) in enumerate([
    ("New Relic", "NerdGraph NRQL · logs, errors, traces"),
    ("GitHub", "Contents + PRs · default branch"),
    ("Jira", "issues, comments, JQL search"),
]):
    x = 58 + i * 348
    box(x, EY + 38, 330, 44, ("#ffffff", "#ccd5de"), r=8)
    text(x + 14, EY + 58, name, size=13.5, weight=600)
    text(x + 14, EY + 74, sub, size=11, fill=MUTED)

# link the agent loop to the sources
arrow(AX + AW / 2, BAND_Y + BAND_H + 6, AX + AW / 2, EY - 6)
arrow(AX + AW / 2 - 24, EY - 6, AX + AW / 2 - 24, BAND_Y + BAND_H + 6)
text(AX + AW / 2 + 12, EY - 18, "queries", size=11.5, fill=MUTED)

# ------------------------------------------------------------- outputs
OY = EY
box(1130, OY, 430, 96, OUTP)
text(1148, OY + 26, "OUTPUTS", size=11.5, weight=700, fill="#1f7a49")
for i, (name, sub) in enumerate([
    ("Jira comment", "only on --post or Approve"),
    (".runs/prevention/*.md", "was it a code bug, how to prevent it"),
    (".runs/*.json", "every query, for audit"),
]):
    y = OY + 38 + i * 19
    text(1148, y + 10, "•", size=13, fill="#1f7a49")
    text(1162, y + 10, name, size=12, weight=600)
    text(1162 + len(name) * 6.6 + 10, y + 10, sub, size=11, fill=MUTED)
arrow(RX + RW / 2, BAND_Y + BAND_H + 6, RX + RW / 2, OY - 6)

# ------------------------------------------------------------ guard rail note
NY = EY + 130
box(40, NY, 1520, 74, ("#fbfcfd", "#e3e8ed"), r=10)
text(58, NY + 26, "WHY IT IS SPLIT THIS WAY", size=11.5, weight=700,
     fill="#6a7481")
lines(58, NY + 48, [
    "Regex, gating, rendering and validation are ordinary testable code — only "
    "the investigation itself is left to the model, and it is bounded on turns, "
    "dollars and tool calls at once.",
], size=13)
lines(58, NY + 66, [
    "Confidence is checked against the run trace rather than taken on trust: a "
    "verdict with no New Relic evidence, or built only on ticket comments, is "
    "downgraded and the reason is printed in the comment.",
], size=13)

# ------------------------------------------------------------------- legend
LY = NY + 102
items = [
    ("deterministic code", DET),
    ("model step", AGENT),
    ("hard gate / guardrail", GUARD),
    ("output", OUTP),
    ("external, read-only", EXT),
]
lx = 40
for label, fill in items:
    f, s = fill
    parts.append(
        f'<rect x="{lx}" y="{LY}" width="16" height="16" rx="4" ry="4" '
        f'fill="{f}" stroke="{s}" stroke-width="1.6"/>'
    )
    text(lx + 24, LY + 13, label, size=12.5, fill=MUTED)
    lx += 34 + len(label) * 7.0

text(W - 40, LY + 13,
     "13 tools · 60-call budget · ~$0.50–0.80 per ticket",
     size=12.5, fill=MUTED, anchor="end")

parts.append("</svg>")
svg = "\n".join(parts)

svg_path = OUT / "architecture.svg"
svg_path.write_text(svg)
print(f"wrote {svg_path}  ({len(svg):,} bytes)")

# ------------------------------------------------------- PNG via headless Chrome
CHROME = next(
    (
        c for c in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            shutil.which("chromium") or "",
            shutil.which("google-chrome") or "",
        ) if c and Path(c).exists()
    ),
    None,
)

if not CHROME:
    print("No Chrome found — SVG written; convert it yourself for the PNG.")
    raise SystemExit(0)

png_path = OUT / "architecture.png"
with tempfile.TemporaryDirectory() as tmp:
    wrapper = Path(tmp) / "d.html"
    wrapper.write_text(
        "<!doctype html><meta charset=utf-8>"
        "<style>html,body{margin:0;padding:0;background:#fff}</style>" + svg
    )
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--default-background-color=FFFFFFFF",
        # Without a virtual time budget, headless Chrome writes the screenshot
        # and then declines to exit.
        "--virtual-time-budget=3000",
        "--run-all-compositor-stages-before-draw",
        f"--screenshot={png_path}",
        f"--window-size={W},{H}",
        "--force-device-scale-factor=2",
        f"--user-data-dir={tmp}/profile",
        wrapper.as_uri(),
    ]
    png_path.unlink(missing_ok=True)
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass        # it may still have written the file before hanging

if not (png_path.exists() and png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"):
    raise SystemExit(f"Chrome did not produce a valid PNG at {png_path}")

print(f"wrote {png_path}  ({png_path.stat().st_size:,} bytes, {W*2}x{H*2})")
