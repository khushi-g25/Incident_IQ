"""Generate the Incident IQ slide deck.

    pip install python-pptx
    python3 docs/make_deck.py

Writes docs/Incident_IQ.pptx — five slides, 16:9, speaker notes on each.
Deliberately short: what it does, how it is built, what it produces, and what
it is not allowed to do. The detail lives in ARCHITECTURE.md, not on a slide.

Palette note: the accents are blue #2a78d6 and amber #e2a531, validated for
colour-vision deficiency (worst adjacent dE 31.2). An earlier red/green pairing
for before/after was rejected — under deuteranopia those two sit at dE 3.5, so a
red/green colourblind viewer in the room could not have told them apart. Amber
falls below 3:1 on white, so it is only ever a fill or rule beside a text label,
never text and never carrying meaning on its own.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

OUT = Path(__file__).resolve().parent
DECK = OUT / "Incident_IQ.pptx"

W, H = Inches(13.333), Inches(7.5)
L = Inches(0.78)                      # left margin
CW = W - L * 2                        # content width

INK = RGBColor(0x0B, 0x0B, 0x0B)
SEC = RGBColor(0x52, 0x51, 0x4E)
MUT = RGBColor(0x83, 0x82, 0x7D)
BLUE = RGBColor(0x2A, 0x78, 0xD6)
AMBER = RGBColor(0xE2, 0xA5, 0x31)
LINE = RGBColor(0xDF, 0xE3, 0xE7)
TILE = RGBColor(0xF6, 0xF8, 0xFA)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

FONT = "Calibri"
MONO = "Consolas"

prs = Presentation()
prs.slide_width, prs.slide_height = W, H
BLANK = prs.slide_layouts[6]


# ----------------------------------------------------------------- primitives
def tb(slide, x, y, w, h, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.paragraphs[0].alignment = align
    return tf


def put(tf, text, size=18, bold=False, color=INK, space_after=6,
        mono=False, first=False, align=None, line=1.25):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.space_after = Pt(space_after)
    p.line_spacing = line
    if align:
        p.alignment = align
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.color.rgb = color
    r.font.name = MONO if mono else FONT
    return p


def rule(slide, x, y, w, color=LINE, h=Pt(1.5)):
    s = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, Emu(int(h)))
    s.fill.solid()
    s.fill.fore_color.rgb = color
    s.line.fill.background()
    s.shadow.inherit = False
    return s


def panel(slide, x, y, w, h, fill=TILE, border=LINE, radius=True):
    shape = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    s = slide.shapes.add_shape(shape, x, y, w, h)
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    if border is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = border
        s.line.width = Pt(1)
    s.shadow.inherit = False
    if radius:
        s.adjustments[0] = 0.06
    return s


def slide(title, kicker=None):
    s = prs.slides.add_slide(BLANK)
    if kicker:
        t = tb(s, L, Inches(0.46), CW, Inches(0.3))
        put(t, kicker.upper(), size=12, bold=True, color=BLUE, first=True,
            space_after=0)
        y = Inches(0.76)
    else:
        y = Inches(0.6)
    t = tb(s, L, y, CW, Inches(0.75))
    put(t, title, size=28, bold=True, first=True, space_after=0, line=1.0)
    rule(s, L, y + Inches(0.62), CW)
    return s


def notes(s, text):
    s.notes_slide.notes_text_frame.text = text


def stat(slide, x, y, w, value, label, sub=None, h=Inches(1.55), accent=BLUE):
    """Stat tile. The number wears ink, not the accent colour — the accent is a
    rule above it, so meaning never rides on hue alone."""
    panel(slide, x, y, w, h)
    rule(slide, x + Inches(0.18), y + Inches(0.2), Inches(0.42), accent, Pt(3))
    tf = tb(slide, x + Inches(0.18), y + Inches(0.36),
            w - Inches(0.36), h - Inches(0.5))
    put(tf, value, size=29, bold=True, first=True, space_after=2, line=1.0)
    put(tf, label, size=12, color=SEC, space_after=1, line=1.12)
    if sub:
        put(tf, sub, size=10, color=MUT, space_after=0, line=1.12)


def bullets(s, items, y=Inches(1.62), size=17, gap=13, width=None):
    tf = tb(s, L, y, width or CW, H - y - Inches(0.6))
    for i, item in enumerate(items):
        if isinstance(item, tuple):
            head, body = item
            put(tf, head, size=size, bold=True, first=(i == 0), space_after=2)
            put(tf, body, size=size - 3, color=SEC, space_after=gap)
        else:
            put(tf, "•  " + item, size=size, first=(i == 0), space_after=gap)
    return tf



# ============================================================ 1 · title
s = prs.slides.add_slide(BLANK)
panel(s, Emu(0), Emu(0), W, Inches(0.09), fill=BLUE, border=None, radius=False)
tf = tb(s, L, Inches(2.35), Inches(10.6), Inches(1.7))
put(tf, "Incident IQ", size=56, bold=True, first=True, space_after=8, line=1.0)
put(tf, "Automated first-pass triage for SQ / EPS tickets",
    size=24, color=SEC, space_after=0)
rule(s, L, Inches(4.25), Inches(1.5), BLUE, Pt(3))
tf = tb(s, L, Inches(4.6), Inches(10.6), Inches(0.6))
put(tf, "Ticket in, evidence-backed root cause out.",
    size=18, color=SEC, first=True, space_after=0)
tf = tb(s, L, Inches(6.5), Inches(10.6), Inches(0.4))
put(tf, "Read-only by construction  ·  nothing posts without a human approving it",
    size=13, color=MUT, first=True, space_after=0)
notes(s, "It does the mechanical first hour of a support ticket and hands an "
         "engineer a starting point with the evidence attached. It never "
         "changes anything.")

# ============================================================ 2 · what it does
s = slide("Give it a ticket key; it does the first pass", "What it does")
steps = [
    ("1", "Reads the ticket",
     "Description, comments and attached logs. Pulls out ids, timestamps and "
     "error signatures."),
    ("2", "Investigates",
     "Finds the real New Relic app names, queries errors and logs over the "
     "ticket's own window, then reads the service code on GitHub."),
    ("3", "Writes it up",
     "Plain-English summary, a recommended fix, and every query it ran — as "
     "one Jira comment."),
]
x = L
cw = (CW - Inches(0.5)) / 3
for num, head_, body_ in steps:
    panel(s, x, Inches(1.85), cw, Inches(3.3))
    tf = tb(s, x + Inches(0.3), Inches(2.1), cw - Inches(0.6), Inches(2.8))
    put(tf, num, size=15, bold=True, color=BLUE, first=True, space_after=8)
    put(tf, head_, size=21, bold=True, space_after=9)
    put(tf, body_, size=14.5, color=SEC, space_after=0, line=1.3)
    x += cw + Inches(0.25)
tf = tb(s, L, Inches(5.55), CW, Inches(0.8))
put(tf, "2–4 minutes  ·  about $0.65 of model time  ·  posts only on approval",
    size=17, bold=True, first=True, space_after=4)
put(tf, "Bounded on turns, dollars and tool calls at once, so a confused run "
        "stops rather than spirals.", size=14, color=SEC, space_after=0)
notes(s, "Three columns map to the four phases on the next slide — intake and "
         "the gate are collapsed into step 1 here.")

# ============================================================ 3 · architecture
s = slide("How it is put together", "Architecture")
img = OUT / "architecture.png"
if img.exists():
    top = Inches(1.62)
    avail_h = H - top - Inches(0.34)
    pic_w = min(CW, Emu(int(avail_h * 16 / 9)))     # source is 16:9
    s.shapes.add_picture(str(img), Emu(int((W - pic_w) / 2)), top, width=pic_w)
notes(s, "Blue is ordinary deterministic code we unit-test. The one amber box "
         "is the only place a model decides anything, and it is wrapped in the "
         "red guardrail strip. Read-only sources along the bottom; the single "
         "write — one Jira comment — on the right.")

# ============================================================ 4 · the output
s = slide("What lands on the ticket", "The output")
left = [
    ("Summary", "Four or five plain sentences. No file paths, no class names."),
    ("Recommended fix", "The change, where to make it, how to confirm it worked."),
    ("Checklist", "What triage established, the fix, and how to prevent a repeat."),
]
right = [
    ("Evidence", "Every claim tied to the query or file that produced it."),
    ("Queries run · logs checked", "In full, so anyone can re-run them."),
    ("Confidence, checked", "Capped against what the run actually verified — "
     "and any downgrade is printed, not hidden."),
]
for col, items in ((L, left), (L + CW / 2 + Inches(0.2), right)):
    tf = tb(s, col, Inches(1.8), CW / 2 - Inches(0.2), Inches(3.6))
    for i, (h_, b_) in enumerate(items):
        put(tf, h_, size=18, bold=True, first=(i == 0), space_after=3)
        put(tf, b_, size=14, color=SEC, space_after=18, line=1.3)
panel(s, L, Inches(5.5), CW, Inches(1.1), fill=RGBColor(0xFD, 0xF7, 0xE9),
      border=AMBER)
tf = tb(s, L + Inches(0.32), Inches(5.78), CW - Inches(0.64), Inches(0.7))
put(tf, "Written for two readers at once: a support lead gets the whole "
        "picture before reaching anything with a file path in it.",
    size=15, bold=True, first=True, space_after=0)
notes(s, "The top of the comment is for a support lead or PM deciding whether "
         "to escalate; the technical detail underneath is for the engineer who "
         "picks it up.")

# ============================================================ 5 · guardrails
s = slide("What it cannot do", "Trust boundaries")
bullets(s, [
    ("It cannot change anything",
     "No writes, no shell, no file edits, no ticket transitions — denied on "
     "every tool call, not by asking the model nicely."),
    ("The clients have no write methods",
     "The boundary is the code, not the prompt. There is no function to call "
     "that would modify New Relic or GitHub."),
    ("One side effect exists",
     "A Jira comment, and only after a human clicks Approve."),
], y=Inches(1.8), size=18, gap=16)
row = [
    ("13", "read-only tools"),
    ("95", "automated tests"),
    ("0", "writes to production"),
    ("1", "click to publish"),
]
x = L
cw = (CW - Inches(0.6)) / 4
for v, lab in row:
    stat(s, x, Inches(5.05), cw, v, lab, h=Inches(1.5))
    x += cw + Inches(0.2)
notes(s, "The question this answers is 'what happens when it is wrong?' — a "
         "wrong comment costs a minute of reading. There is no path to a wrong "
         "write.")

prs.save(DECK)
print(f"wrote {DECK}  ({DECK.stat().st_size:,} bytes, {len(prs.slides._sldIdLst)} slides)")
