"""Generate the Incident IQ slide deck.

    pip install python-pptx
    python3 docs/make_deck.py

Writes docs/Incident_IQ.pptx — 16:9, with speaker notes on every slide.

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
tf = tb(s, L, Inches(2.25), Inches(10.4), Inches(1.6))
put(tf, "Incident IQ", size=54, bold=True, first=True, space_after=6, line=1.0)
put(tf, "Automated first-pass triage for SQ / EPS tickets",
    size=23, color=SEC, space_after=0)
rule(s, L, Inches(4.05), Inches(1.5), BLUE, Pt(3))
tf = tb(s, L, Inches(4.35), Inches(10.4), Inches(1.0))
put(tf, "Ticket in, evidence-backed root cause out — with the queries that "
        "prove it, a recommended fix, and a note on how to stop it recurring.",
    size=16, color=SEC, first=True, space_after=0)
tf = tb(s, L, Inches(6.5), Inches(10.4), Inches(0.4))
put(tf, "Read-only by construction  ·  nothing posts without a human approving it",
    size=13, color=MUT, first=True, space_after=0)
notes(s, "One-line pitch: this does the first hour of triage on a support "
         "ticket — the part that is mechanical — and hands an engineer a "
         "starting point with evidence attached. It never changes anything.")

# ============================================================ 2 · problem
s = slide("First-pass triage is mechanical, and it is expensive", "The problem")
bullets(s, [
    ("Every ticket starts the same way",
     "Read the ticket. Work out which service. Find the right app name in New "
     "Relic. Write the query. Read the code. Most of that is lookup, not "
     "judgement."),
    ("The context is scattered across three systems",
     "Jira has the report, New Relic has the behaviour, GitHub has the cause. "
     "Correlating them by hand is where the time goes."),
    ("Knowledge leaves with whoever fixed it",
     "The cause lands in a comment thread, not a runbook. The next person with "
     "the same symptom starts from zero."),
    ("The cost is the engineer's attention, not the ticket",
     "An interrupted engineer pays far more than the ten minutes the lookup "
     "took."),
])
notes(s, "Frame it as lookup versus judgement. We are not trying to replace the "
         "diagnosis — we are trying to delete the lookup that precedes it, and "
         "to stop losing what we learn.")

# ============================================================ 3 · what it does
s = slide("Give it a ticket key; it does the first pass", "What it does")
steps = [
    ("1", "Reads the ticket", "Description, comments, and any attached log files. "
     "Pulls out ids, timestamps and error signatures in code, not in the model."),
    ("2", "Investigates", "Discovers the real New Relic app names, queries errors "
     "and logs over the ticket's own time window, then reads the service code on "
     "GitHub to explain what it found."),
    ("3", "Writes it up", "A plain-English summary, a recommended fix, a checklist, "
     "and every query it ran — posted as one Jira comment, on approval."),
]
x = L
cw = (CW - Inches(0.5)) / 3
for num, head, body in steps:
    panel(s, x, Inches(1.75), cw, Inches(3.5))
    tf = tb(s, x + Inches(0.3), Inches(2.0), cw - Inches(0.6), Inches(3.0))
    put(tf, num, size=15, bold=True, color=BLUE, first=True, space_after=8)
    put(tf, head, size=20, bold=True, space_after=9)
    put(tf, body, size=14, color=SEC, space_after=0, line=1.3)
    x += cw + Inches(0.25)
tf = tb(s, L, Inches(5.6), CW, Inches(0.9))
put(tf, "Typical run: 2–4 minutes, 50–65 tool calls, $0.50–$0.80 of model time.",
    size=16, bold=True, first=True, space_after=4)
put(tf, "Bounded three ways at once — turns, dollars, and tool calls — so a "
        "confused run stops rather than spirals.", size=14, color=SEC,
    space_after=0)
notes(s, "The three columns map exactly to the four phases in the architecture "
         "slide; intake and the gate are collapsed into step 1 here.")

# ============================================================ 4 · architecture
s = slide("How it is put together", "Architecture")
img = OUT / "architecture.png"
if img.exists():
    top = Inches(1.62)
    avail_h = H - top - Inches(0.34)
    pic_w = min(CW, Emu(int(avail_h * 16 / 9)))     # source is 16:9
    s.shapes.add_picture(str(img), Emu(int((W - pic_w) / 2)), top, width=pic_w)
notes(s, "Everything blue is ordinary deterministic code we unit-test. The one "
         "amber box is the only place a model makes a decision, and it is "
         "wrapped in the red guardrail strip. Read-only sources along the "
         "bottom; the single write — one Jira comment — on the right.")

# ============================================================ 5 · method
s = slide("The method it is made to follow", "How it investigates")
bullets(s, [
    ("1 · Do not adopt anyone's conclusion",
     "Ticket comments and prior tickets are labelled as unverified claims. They "
     "decide what to look for, never what to believe."),
    ("2 · Discover before filtering",
     "Repo name, service name and New Relic app name are all different. A guessed "
     "app name returns zero rows that look exactly like 'nothing is broken'."),
    ("3 · Find the failure signature",
     "Errors and logs over the ticket's own window, down to an exception class and "
     "ideally a trace id."),
    ("4 · Explain it in the code",
     "Search for the literal log message, then blame and PR history for the line "
     "that produced it."),
    ("5 · Converge, or stop and say so",
     "A root cause only when logs, data and code agree. Otherwise it reports what "
     "it narrowed down — which is a good outcome, not a failure."),
], size=16, gap=11)
notes(s, "Step 1 is the one that took the most work. Left neutral, a confident "
         "ticket comment becomes the model's answer and the whole run turns "
         "into a paraphrase of what someone already wrote.")

# ============================================================ 6 · the comment
s = slide("What lands on the ticket", "The output")
left = [
    ("Summary", "Four or five plain sentences. No file paths, no class names — "
     "written for whoever decides whether to escalate."),
    ("Recommended fix", "The change, where to make it, how to confirm it worked, "
     "and any interim workaround."),
    ("Checklist", "Tickable items: what triage established, the fix, and at least "
     "one prevention item."),
]
right = [
    ("Read this before acting on it", "Any confidence the system reduced, and why "
     "it reduced it."),
    ("Technical detail & evidence", "Root cause, code location, and each claim "
     "tied to the query or file that produced it."),
    ("Queries run · logs checked", "Every query in full, so anyone can re-run it. "
     "Plus what log searches were tried and what they returned."),
]
for col, items in ((L, left), (L + CW / 2 + Inches(0.2), right)):
    tf = tb(s, col, Inches(1.68), CW / 2 - Inches(0.2), Inches(4.4))
    for i, (head, body) in enumerate(items):
        put(tf, head, size=17, bold=True, first=(i == 0), space_after=3)
        put(tf, body, size=13.5, color=SEC, space_after=14, line=1.28)
panel(s, L, Inches(5.85), CW, Inches(0.95), fill=RGBColor(0xFD, 0xF7, 0xE9),
      border=AMBER)
tf = tb(s, L + Inches(0.3), Inches(6.06), CW - Inches(0.6), Inches(0.6))
put(tf, "Ordered widest-reader-first: a support lead gets the whole picture "
        "before reaching anything with a file path in it.",
    size=14.5, bold=True, first=True, space_after=0)
notes(s, "Two audiences, one comment. The top half is for a support lead or PM; "
         "the bottom half is for the engineer who picks it up.")

# ============================================================ 7 · guardrails
s = slide("What it cannot do", "Trust boundaries")
bullets(s, [
    ("It cannot change anything",
     "No writes, no shell, no file edits, no ticket transitions. Denied at the "
     "hook on every single tool call, not by asking the model nicely."),
    ("The clients have no write methods",
     "The security boundary is the code, not the prompt. There is no function to "
     "call that would modify New Relic or GitHub."),
    ("One side effect exists",
     "A Jira comment — and only after --post on the command line or a human "
     "clicking Approve in the review UI."),
    ("Personal data never reaches the model",
     "Emails and card numbers are replaced with stable placeholders on the way "
     "in and restored on the way out."),
], size=16.5, gap=13, width=Inches(8.2))
x = L + Inches(8.6)
stat(s, x, Inches(1.75), Inches(3.15), "60", "tool-call budget per run",
     "then it must write up what it has", h=Inches(1.7))
stat(s, x, Inches(3.6), Inches(3.15), "95", "automated tests",
     "guardrails, query repair, rendering", h=Inches(1.7))
stat(s, x, Inches(5.45), Inches(3.15), "0", "writes to production",
     "by construction, not by policy", h=Inches(1.7))
notes(s, "The question this slide answers is 'what happens when it is wrong?' — "
         "and the answer is that a wrong comment costs a minute of reading. "
         "There is no path to a wrong write.")

# ============================================================ 8 · honesty
s = slide("Confidence is checked, not taken on trust", "Why you can believe it")
bullets(s, [
    ("The model declares what it actually verified",
     "Logs, code, data — confirmed, checked-and-empty, or never checked."),
    ("That claim is cross-checked against the run trace",
     "If it says it confirmed the logs but no log query returned a line, the "
     "report is downgraded automatically."),
    ("A report built only on ticket comments cannot claim a root cause",
     "It is reduced to 'narrowed, not confirmed' at low confidence — because a "
     "paraphrase of the ticket is not a diagnosis."),
    ("Every downgrade is printed in the comment",
     "Never applied silently. The reader sees the reduction and the reason for "
     "it, so a hedged report is the system working."),
], size=16.5, gap=13)
notes(s, "This is the slide that matters for adoption. The failure mode people "
         "fear is a confident wrong answer sending someone down the wrong path "
         "at 2am. We cap confidence mechanically so that cannot happen quietly.")

# ============================================================ 9 · hardening
s = slide("What hardening found", "Measured against 11 recorded runs")
tf = tb(s, L, Inches(1.6), CW, Inches(0.5))
put(tf, "The first build looked like it worked. Reading the run traces rather "
        "than the output told a different story.", size=15.5, color=SEC,
    first=True, space_after=0)
row = [
    ("3 of 4", "observability tools silently broken",
     "failed on every run, returning nothing"),
    ("65%", "of data queries returned zero rows",
     "guessed app names, read as 'no errors'"),
    ("29 → 4", "log queries that returned a line",
     "logs were never really searched"),
    ("14", "queries lost to one NRQL mistake",
     "valid SQL, invalid NRQL — now auto-repaired"),
]
x = L
cw = (CW - Inches(0.6)) / 4
for v, lab, sub in row:
    stat(s, x, Inches(2.35), cw, v, lab, sub, h=Inches(2.0))
    x += cw + Inches(0.2)
panel(s, L, Inches(4.6), CW, Inches(1.55), fill=RGBColor(0xFD, 0xF7, 0xE9),
      border=AMBER)
tf = tb(s, L + Inches(0.32), Inches(4.82), CW - Inches(0.64), Inches(1.2))
put(tf, "The lesson we kept relearning", size=15, bold=True, first=True,
    space_after=5)
put(tf, "None of this was visible in the reports. The agent wrote confident "
        "prose either way — it simply had nothing underneath it. Everything we "
        "now check automatically came from reading traces, not read-throughs.",
    size=14, color=SEC, space_after=0, line=1.3)
notes(s, "Be candid here. The point is not that it was broken; it is that a "
         "plausible-sounding report is not evidence of a working system, which "
         "is exactly why the confidence checks and the evidence appendix exist.")

# ============================================================ 10 · now
s = slide("Where it stands now", "Current state")
row = [
    ("13", "read-only tools", "discovery, telemetry, code, history"),
    ("~$0.65", "median cost per ticket", "2–4 minutes end to end"),
    ("95", "tests passing", "every fix has a regression test"),
    ("1", "action needed to publish", "a human clicking Approve"),
]
x = L
cw = (CW - Inches(0.6)) / 4
for v, lab, sub in row:
    stat(s, x, Inches(1.75), cw, v, lab, sub, h=Inches(2.0))
    x += cw + Inches(0.2)
bullets(s, [
    ("Investigation now leads with telemetry, not code archaeology",
     "A representative recent run made 27 New Relic queries against 4 GitHub "
     "calls — the reverse of where it started."),
    ("Queries are published in full",
     "Both as a deep link and as copy-pasteable text, so a claim can always be "
     "checked independently."),
    ("Every run leaves a prevention document behind",
     "Written whether or not the cause was a code bug."),
], y=Inches(4.0), size=16, gap=12)
notes(s, "The 27-versus-4 figure is the one to say out loud: it is the clearest "
         "single indicator that it is investigating rather than guessing from "
         "source code.")

# ============================================================ 11 · prevention
s = slide("Every run leaves something behind", "Preventing the repeat")
bullets(s, [
    ("Was this a code bug — yes, no, or not established",
     "'No' is a real answer. A misconfiguration the code permits by design is "
     "the one most likely to recur unrecorded."),
    ("What to change so the class of failure goes away",
     "Not just today's instance of it."),
    ("How we would catch it next time",
     "The specific test, alert or dashboard — with the condition and the "
     "threshold, not 'add monitoring'."),
    ("A paste-able runbook entry",
     "The symptom as it presents, and the first thing to check. Written for "
     "whoever picks up the next ticket that looks like this one."),
], size=16.5, gap=13, width=Inches(8.4))
panel(s, L + Inches(8.8), Inches(1.75), Inches(2.95), Inches(3.9))
tf = tb(s, L + Inches(9.05), Inches(2.0), Inches(2.45), Inches(3.4))
put(tf, "Saved per run", size=12, bold=True, color=BLUE, first=True,
    space_after=10)
for path, desc in [
    (".runs/prevention/", "the document"),
    (".runs/*.json", "every query, for audit"),
    ("Jira comment", "the summary and the fix"),
]:
    put(tf, path, size=13, bold=True, mono=True, space_after=2)
    put(tf, desc, size=12, color=SEC, space_after=12)
notes(s, "This is the compounding part. One triage saves an hour; a year of "
         "prevention notes changes how fast the team recognises a repeat.")

# ============================================================ 12 · rollout
s = slide("How we roll it out", "Plan")
phases = [
    ("Now", "Dry run", "Run it against already-resolved tickets and compare its "
     "verdict to the real cause. Nothing posts."),
    ("Next", "Human-approved", "Support runs it from the review UI. A person "
     "reads every comment before it goes on a ticket."),
    ("Then", "Labelled subset", "Webhook triage for tickets carrying one label, "
     "so we widen by ticket type rather than all at once."),
    ("Later", "Default first pass", "Only once the false-cause rate is low "
     "enough that reading the comment beats ignoring it."),
]
x = L
cw = (CW - Inches(0.75)) / 4
for i, (when, title, body) in enumerate(phases):
    accent = BLUE if i < 2 else LINE
    panel(s, x, Inches(1.85), cw, Inches(3.1))
    rule(s, x + Inches(0.22), Inches(2.1), Inches(0.45), accent, Pt(3))
    tf = tb(s, x + Inches(0.22), Inches(2.3), cw - Inches(0.44), Inches(2.6))
    put(tf, when.upper(), size=11.5, bold=True, color=BLUE if i < 2 else MUT,
        first=True, space_after=6)
    put(tf, title, size=19, bold=True, space_after=8)
    put(tf, body, size=13.5, color=SEC, space_after=0, line=1.3)
    x += cw + Inches(0.25)
tf = tb(s, L, Inches(5.35), CW, Inches(1.0))
put(tf, "The gate between each step is the same question:", size=15, bold=True,
    first=True, space_after=4)
put(tf, "on recently resolved tickets, how often does it name the cause an "
        "engineer would have named? We widen when that number earns it — not on "
        "a date.", size=14.5, color=SEC, space_after=0, line=1.3)
notes(s, "Resist a date-driven rollout. The measure is agreement with known "
         "causes on resolved tickets, and we should be willing to stay at "
         "human-approved indefinitely if that is what the number says.")

# ============================================================ 13 · asks
s = slide("What we need next", "Next steps")
bullets(s, [
    ("A calibration set — 20 to 30 resolved tickets",
     "With their real root causes, so we can measure agreement instead of "
     "guessing at it. This is the single highest-value thing we can be given."),
    ("Service ownership in the playbook",
     "App names, repos and owning channel per service. Most remaining mistakes "
     "are the agent not knowing our estate, not the agent reasoning badly."),
    ("Confirmation that log forwarding is what we think it is",
     "Log searches come back empty far more often than they should. That may be "
     "a data-availability problem rather than a quiet system."),
    ("A decision on scope",
     "Which ticket types we point it at first."),
], size=16.5, gap=14)
notes(s, "Close on the calibration set. Without resolved tickets to measure "
         "against, every claim about accuracy on this deck is an anecdote, "
         "including the good ones.")

prs.save(DECK)
print(f"wrote {DECK}  ({DECK.stat().st_size:,} bytes, {len(prs.slides.__iter__.__self__._sldIdLst)} slides)")
