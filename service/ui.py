"""Local approval UI: paste a Jira link, run the agent, approve or reject.

Not the webhook path — this is a human-in-the-loop tool for one ticket at a
time, run locally (`uvicorn service.ui:app --port 8090 --reload`). Use
`--reload`: without it the prompt, schema and tool changes you just made stay
cached in the running process, and you spend an hour debugging output that the
current code no longer produces. It never posts to
Jira on its own; `triage()` always runs with post=False here, and a comment
only goes out when a human clicks Approve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from triage.agent import LOG_SINK, triage
from triage.clients.jira import JiraClient
from triage.config import Settings
from triage.simplify import _strip_markdown, simplify

log = logging.getLogger("triage.ui")

_TICKET_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")

# run_id -> {ticket, markdown, report} — resolved runs awaiting approve/reject.
_RUNS: dict[str, dict[str, Any]] = {}

# request_id -> {status, logs, result} — one entry per /api/run call, polled
# by /api/stream while the agent is working.
_JOBS: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.from_env()
    log.info("loaded playbook %s", app.state.settings.playbook.name)
    yield


app = FastAPI(lifespan=lifespan)


class RunRequest(BaseModel):
    ticket: str
    extra_prompt: str | None = None


def _extract_ticket(raw: str) -> str:
    m = _TICKET_RE.search(raw.strip().upper())
    if not m:
        raise HTTPException(422, f"Couldn't find a ticket key (e.g. SQ-1234) in {raw!r}")
    return m.group(1)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


@app.post("/api/run")
async def run(req: RunRequest) -> dict[str, str]:
    ticket = _extract_ticket(req.ticket)
    request_id = uuid.uuid4().hex[:12]
    _JOBS[request_id] = {"status": "running", "logs": [], "result": None}
    asyncio.create_task(_run_job(request_id, ticket, req.extra_prompt))
    return {"request_id": request_id, "ticket": ticket}


async def _run_job(request_id: str, ticket: str, extra_prompt: str | None) -> None:
    job = _JOBS[request_id]
    logs: list[str] = job["logs"]
    settings: Settings = app.state.settings
    token = LOG_SINK.set(logs)
    try:
        outcome = await triage(ticket, settings, post=False, extra_context=extra_prompt)

        if outcome.report is None:
            summary = _strip_markdown(outcome.markdown)  # gate message
        else:
            logs.append("building plain-English summary ...")
            summary = await simplify(outcome.report, settings)

        _RUNS[outcome.run_id] = {
            "ticket": ticket,
            "markdown": outcome.markdown,
            "report": outcome.report,
        }
        job["result"] = {
            "run_id": outcome.run_id,
            "ticket": ticket,
            "verdict": (outcome.report or {}).get("verdict"),
            "confidence": (outcome.report or {}).get("confidence"),
            "summary": summary,
            # The comment body, exactly as it will be posted to Jira. Without
            # this the UI had nothing to show under "Full report".
            "markdown": outcome.markdown,
            "report": outcome.report,
            "cost_usd": outcome.cost_usd,
            "skipped_reason": outcome.skipped_reason,
        }
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - surfaced to the UI, not raised
        log.exception("triage failed for %s", ticket)
        job["result"] = {"error": str(e)}
        job["status"] = "error"
    finally:
        LOG_SINK.reset(token)


@app.get("/api/stream/{request_id}")
async def stream(request_id: str) -> StreamingResponse:
    if request_id not in _JOBS:
        raise HTTPException(404, "Unknown request_id")

    async def gen():
        sent = 0
        while True:
            job = _JOBS[request_id]
            logs = job["logs"]
            while sent < len(logs):
                yield f"data: {json.dumps({'type': 'log', 'line': logs[sent]})}\n\n"
                sent += 1
            if job["status"] != "running":
                yield f"data: {json.dumps({'type': job['status'], 'result': job['result']})}\n\n"
                del _JOBS[request_id]
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/approve/{run_id}")
async def approve(run_id: str) -> dict[str, Any]:
    run_data = _RUNS.pop(run_id, None)
    if run_data is None:
        raise HTTPException(404, "Unknown or already-resolved run_id")
    settings: Settings = app.state.settings
    jira = JiraClient(settings.jira)
    comment_id = jira.add_comment(run_data["ticket"], run_data["markdown"])
    return {"status": "posted", "ticket": run_data["ticket"], "comment_id": comment_id}


@app.post("/api/reject/{run_id}")
async def reject(run_id: str) -> dict[str, Any]:
    if _RUNS.pop(run_id, None) is None:
        raise HTTPException(404, "Unknown or already-resolved run_id")
    return {"status": "discarded"}


_PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Incident IQ — triage &amp; approve</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 780px; margin: 40px auto; padding: 0 20px;
    color: #1a1a1a; background: #f7f8fa;
  }
  h2 { margin-bottom: 4px; }
  .subtitle { color: #57606a; margin-top: 0; margin-bottom: 24px; font-size: 14px; }
  .card {
    background: #fff; border: 1px solid #e1e4e8; border-radius: 8px;
    padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }
  label { display: block; font-size: 13px; font-weight: 600; color: #333; margin-bottom: 4px; }
  textarea, input {
    width: 100%; font-family: inherit; font-size: 14px; padding: 9px 10px;
    margin-bottom: 14px; border: 1px solid #d0d7de; border-radius: 6px;
    background: #fff;
  }
  textarea:disabled, input:disabled { background: #f0f1f3; color: #888; }
  textarea:focus, input:focus { outline: none; border-color: #6a5cff; box-shadow: 0 0 0 3px rgba(106,92,255,0.15); }
  button {
    padding: 9px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
    margin-right: 8px; border: none; border-radius: 6px; transition: opacity .15s;
  }
  button:disabled { cursor: not-allowed; opacity: 0.55; }
  .btn-primary { background: #6a5cff; color: #fff; }
  .btn-primary:hover:not(:disabled) { background: #5a4de0; }
  .btn-approve { background: #1a7f37; color: #fff; }
  .btn-approve:hover:not(:disabled) { background: #146c2e; }
  .btn-reject { background: #fff; color: #cf222e; border: 1px solid #cf222e; }
  .btn-reject:hover:not(:disabled) { background: #fff0f0; }
  #result { display: none; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 12px; margin-left: 8px; font-weight: 600; }
  .verdict-root_cause_identified { background: #d4edda; color: #155724; }
  .verdict-narrowed_not_confirmed, .verdict-insufficient_information { background: #fff3cd; color: #856404; }
  .verdict-not_reproducible_from_data, .verdict-not_a_bug { background: #e2e3e5; color: #383d41; }
  #summary { line-height: 1.7; font-size: 15px; color: #24292f; }
  #summary p { margin: 0 0 12px 0; }
  #summary p:last-child { margin-bottom: 0; }
  #status { margin-top: 14px; font-weight: 600; }
  #meta { font-size: 12.5px; color: #777; margin-top: 6px; }
  details { margin-top: 14px; }
  summary { cursor: pointer; font-size: 13px; color: #57606a; }
  pre { white-space: pre-wrap; background: #f6f8fa; padding: 12px; border-radius: 6px; font-size: 12.5px; }
  .section-title {
    margin: 22px 0 8px; padding-top: 16px; border-top: 1px solid #e1e4e8;
    font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: #57606a;
  }
  .report { font-size: 14.5px; line-height: 1.65; color: #24292f; }
  .report h2, .report h3, .report h4 {
    margin: 20px 0 8px; line-height: 1.3;
  }
  .report h2 { font-size: 17px; }
  .report h3 { font-size: 16px; }
  /* `###` in the report renders here — it is the main section level, so it
     needs to read as a heading, not as a caption. */
  .report h4 { font-size: 14.5px; color: #24292f; font-weight: 600; }
  .report > *:first-child { margin-top: 0; }
  .report p { margin: 0 0 10px; }
  .report ul, .report ol { margin: 0 0 12px; padding-left: 22px; }
  .report li { margin-bottom: 5px; }
  .report code {
    background: #eff1f3; padding: 1.5px 5px; border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12.5px;
    overflow-wrap: anywhere;
  }
  .report a { color: #6a5cff; }
  .report hr { border: none; border-top: 1px solid #e1e4e8; margin: 18px 0; }
  .report table {
    border-collapse: collapse; width: 100%; margin: 0 0 14px; font-size: 12.5px;
    display: block; overflow-x: auto;
  }
  .report th, .report td {
    border: 1px solid #e1e4e8; padding: 6px 9px; text-align: left; vertical-align: top;
  }
  .report th { background: #f6f8fa; font-weight: 600; }
  .muted { color: #888; font-style: italic; }
  .report ul.tasklist { list-style: none; padding-left: 2px; }
  .report li.task { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
  .report li.task input { margin-top: 3px; flex-shrink: 0; }
  .report li.task .done { color: #57606a; }
  /* The fix panel is the actionable part of the page, so it gets a tinted
     card rather than sitting flush with the surrounding prose. */
  .fix {
    background: #f4f8f5; border: 1px solid #cfe3d6; border-left: 3px solid #1a7f37;
    border-radius: 6px; padding: 14px 16px;
  }
  .fix > *:last-child { margin-bottom: 0; }
  .fix .pill {
    display: inline-block; background: #dff0e4; color: #14532d;
    padding: 2px 9px; border-radius: 10px; font-size: 12px; font-weight: 600;
    margin: 0 0 10px;
  }
  .fix .urg {
    display: inline-block; background: #e7e9ec; color: #3a3f45;
    padding: 1px 7px; border-radius: 8px; font-size: 11.5px; font-weight: 600;
    margin-right: 6px;
  }
  #fixSection { display: none; }
  #progress { display: none; margin-top: 4px; }
  #logBox {
    background: #0d1117; color: #c9d1d9; font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12.5px; line-height: 1.6; padding: 12px; border-radius: 6px;
    height: 260px; overflow-y: auto; white-space: pre-wrap;
  }
  #progressLabel { font-size: 13px; color: #555; margin-bottom: 8px; display: flex; justify-content: space-between; }
</style>
</head>
<body>
  <h2>Incident IQ — triage &amp; approve</h2>
  <p class="subtitle">Paste a Jira link or key, optionally add extra context, then run the
     agent. Nothing is posted to Jira until you click Approve.</p>

  <div class="card">
    <label>Jira ticket (link or key)</label>
    <input id="ticket" placeholder="https://yourcompany.atlassian.net/browse/SQ-1234 or SQ-1234">

    <label>Extra context for the agent (optional)</label>
    <textarea id="prompt" rows="3" placeholder="e.g. focus on the payments service, ignore the UI"></textarea>

    <button id="runBtn" class="btn-primary" onclick="runTriage()">Run triage</button>
  </div>

  <div class="card" id="progress">
    <div id="progressLabel"><span id="progressText">Working ...</span><span id="elapsed"></span></div>
    <div id="logBox"></div>
  </div>

  <div class="card" id="result">
    <h3>Ticket: <span id="rTicket"></span>
      <span class="badge" id="rVerdict"></span>
    </h3>
    <div id="summary"></div>

    <div id="fixSection">
      <h4 class="section-title">Recommended fix</h4>
      <div id="fixCard" class="report fix"></div>
    </div>

    <div style="margin-top: 16px;">
      <button id="approveBtn" class="btn-approve" onclick="decide('approve')">Approve &amp; post to Jira</button>
      <button id="rejectBtn" class="btn-reject" onclick="decide('reject')">Reject (discard)</button>
    </div>
    <div id="status"></div>
    <div id="meta"></div>

    <h4 class="section-title">Comment that will be posted</h4>
    <div id="fullReport" class="report"></div>

    <details>
      <summary>Raw markdown</summary>
      <pre id="rawMarkdown"></pre>
    </details>
  </div>

<script>
let currentRunId = null;
let currentStream = null;
let elapsedTimer = null;
let startedAt = null;

function renderSummary(text) {
  const el = document.getElementById('summary');
  el.innerHTML = '';
  text.split(/\n+/).map(s => s.trim()).filter(Boolean).forEach(para => {
    const p = document.createElement('p');
    p.textContent = para;
    el.appendChild(p);
  });
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Inline spans only. Escape first, so nothing in a report body can inject
// markup into this page.
function inlineMd(s) {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');
}

// Block-level renderer covering what render_markdown emits: headings, lists,
// pipe tables, rules and paragraphs.
function renderMarkdown(md) {
  if (!md) return '<p class="muted">No report was produced for this run.</p>';
  const lines = md.split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const t = line.trim();

    if (!t) { i++; continue; }

    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = Math.min(h[1].length + 1, 6);   // '#' -> h2, so page h2 stays top
      out.push('<h' + lvl + '>' + inlineMd(h[2]) + '</h' + lvl + '>');
      i++; continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { out.push('<hr>'); i++; continue; }

    // pipe table
    if (/^\|.*\|$/.test(t) && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i+1].trim())) {
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      let html = '<table><thead><tr>' +
        cells(t).map(c => '<th>' + inlineMd(c) + '</th>').join('') +
        '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && /^\|.*\|$/.test(lines[i].trim())) {
        html += '<tr>' + cells(lines[i]).map(c => '<td>' + inlineMd(c) + '</td>').join('') + '</tr>';
        i++;
      }
      out.push(html + '</tbody></table>');
      continue;
    }

    const isBullet = l => /^\s*[-*+]\s+/.test(l);
    const isOrdered = l => /^\s*\d+[.)]\s+/.test(l);
    const TASK = /^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/;

    // Checklist before plain bullets, so items render as real boxes.
    if (TASK.test(line)) {
      const items = [];
      let m;
      while (i < lines.length && (m = lines[i].match(TASK))) {
        const done = m[1].toLowerCase() === 'x';
        items.push('<li class="task"><input type="checkbox" disabled' +
                   (done ? ' checked' : '') + '> <span' +
                   (done ? ' class="done"' : '') + '>' + inlineMd(m[2]) + '</span></li>');
        i++;
      }
      out.push('<ul class="tasklist">' + items.join('') + '</ul>');
      continue;
    }

    if (isBullet(line) || isOrdered(line)) {
      const ordered = isOrdered(line) && !isBullet(line);
      const test = ordered ? isOrdered : isBullet;
      const strip = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
      const items = [];
      while (i < lines.length && test(lines[i])) {
        items.push('<li>' + inlineMd(lines[i].replace(strip, '')) + '</li>');
        i++;
      }
      out.push((ordered ? '<ol>' : '<ul>') + items.join('') + (ordered ? '</ol>' : '</ul>'));
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim()) {
      const n = lines[i];
      if (para.length && (/^#{1,6}\s/.test(n.trim()) || isBullet(n) || isOrdered(n) ||
                          /^\|.*\|$/.test(n.trim()) || /^(-{3,})$/.test(n.trim()))) break;
      para.push(n.trim());
      i++;
    }
    out.push('<p>' + inlineMd(para.join(' ')) + '</p>');
  }
  return out.join('\n');
}

const FIX_TYPES = {
  code_change: 'a code change',
  config_change: 'a configuration change',
  data_correction: 'a data correction',
  infrastructure: 'an infrastructure change',
  no_change_needed: 'no change needed',
  needs_more_investigation: 'more investigation needed before a fix is clear'
};
const URGENCY = { now: 'Now', this_sprint: 'This sprint', backlog: 'Backlog' };

// The solution, lifted out of the structured report so it has its own place on
// the page instead of only existing inside the comment body.
function renderFix(report) {
  const el = document.getElementById('fixCard');
  const rc = (report && report.root_cause) || {};
  const parts = [];

  if (!rc.suggested_fix) {
    parts.push('<p class="muted">The agent did not propose a fix for this ticket. ' +
               'Check the next actions in the full comment below.</p>');
  } else {
    if (rc.fix_type) {
      parts.push('<p class="pill">' + escapeHtml(FIX_TYPES[rc.fix_type] || rc.fix_type) + '</p>');
    }
    parts.push('<p>' + inlineMd(rc.suggested_fix) + '</p>');

    const loc = rc.code_location;
    if (loc && loc.file) {
      const where = loc.file + (loc.line ? ':' + loc.line : '');
      parts.push('<p><strong>Where.</strong> <code>' + escapeHtml(where) + '</code>' +
                 (loc.why ? ' — ' + inlineMd(loc.why) : '') + '</p>');
    }
    if (rc.fix_verification) {
      parts.push('<p><strong>How to confirm it worked.</strong> ' + inlineMd(rc.fix_verification) + '</p>');
    }
    if (rc.workaround) {
      parts.push('<p><strong>Interim workaround.</strong> ' + inlineMd(rc.workaround) + '</p>');
    }
  }

  const actions = (report && report.next_actions) || [];
  if (actions.length) {
    parts.push('<p><strong>Next actions</strong></p><ul>' + actions.map(a =>
      '<li><span class="urg">' + escapeHtml(URGENCY[a.urgency] || a.urgency || 'This sprint') +
      '</span> ' + inlineMd(a.action || '') +
      (a.owner_hint ? ' — <em>' + escapeHtml(a.owner_hint) + '</em>' : '') + '</li>'
    ).join('') + '</ul>');
  }

  el.innerHTML = parts.join('\n');
  document.getElementById('fixSection').style.display = report ? 'block' : 'none';
}

function appendLog(line) {
  const box = document.getElementById('logBox');
  const div = document.createElement('div');
  div.textContent = line;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setRunning(isRunning) {
  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = isRunning;
  runBtn.textContent = isRunning ? 'Running...' : 'Run triage';
  document.getElementById('ticket').disabled = isRunning;
  document.getElementById('prompt').disabled = isRunning;
  if (isRunning) {
    startedAt = Date.now();
    elapsedTimer = setInterval(() => {
      const s = Math.floor((Date.now() - startedAt) / 1000);
      document.getElementById('elapsed').textContent = s + 's';
    }, 1000);
  } else if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

async function runTriage() {
  const ticket = document.getElementById('ticket').value;
  const prompt = document.getElementById('prompt').value;
  if (!ticket.trim()) { alert('Enter a ticket link or key'); return; }

  setRunning(true);
  document.getElementById('result').style.display = 'none';
  document.getElementById('status').textContent = '';
  document.getElementById('logBox').innerHTML = '';
  document.getElementById('progressText').textContent = 'Working ...';
  document.getElementById('elapsed').textContent = '0s';
  document.getElementById('progress').style.display = 'block';

  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticket: ticket, extra_prompt: prompt || null})
    });
    const data = await r.json();
    if (!r.ok) { alert(data.detail || 'Run failed'); setRunning(false); return; }

    if (currentStream) currentStream.close();
    currentStream = new EventSource('/api/stream/' + data.request_id);

    currentStream.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'log') {
        appendLog(msg.line);
      } else if (msg.type === 'done') {
        currentStream.close();
        document.getElementById('progressText').textContent = 'Done.';
        showResult(msg.result);
        setRunning(false);
      } else if (msg.type === 'error') {
        currentStream.close();
        appendLog('ERROR: ' + (msg.result && msg.result.error));
        alert('Run failed: ' + (msg.result && msg.result.error));
        setRunning(false);
      }
    };
    currentStream.onerror = () => {
      currentStream.close();
      document.getElementById('progressText').textContent = 'Stream disconnected.';
      setRunning(false);
    };
  } catch (e) {
    alert('Request failed: ' + e);
    setRunning(false);
  }
}

function showResult(data) {
  currentRunId = data.run_id;
  document.getElementById('rTicket').textContent = data.ticket;
  const badge = document.getElementById('rVerdict');
  const VERDICTS = {
    root_cause_identified: 'cause identified',
    narrowed_not_confirmed: 'narrowed, not confirmed',
    not_reproducible_from_data: 'not reproducible from data',
    insufficient_information: 'insufficient information',
    not_a_bug: 'not a bug'
  };
  const CONF = { high: 'high confidence', medium: 'moderate confidence', low: 'low confidence' };
  badge.textContent = data.verdict
    ? VERDICTS[data.verdict] + (data.confidence ? ' · ' + CONF[data.confidence] : '')
    : (data.skipped_reason ? 'skipped' : 'no report');
  badge.className = 'badge verdict-' + (data.verdict || 'none');
  renderSummary(data.summary);
  renderFix(data.report);
  document.getElementById('rawMarkdown').textContent = data.markdown || '';
  document.getElementById('fullReport').innerHTML = renderMarkdown(data.markdown || '');
  document.getElementById('status').textContent = '';
  document.getElementById('meta').textContent =
    'run ' + data.run_id + ' · cost $' + data.cost_usd.toFixed(3);
  document.getElementById('result').style.display = 'block';

  const hasReport = !!data.verdict;
  const approveBtn = document.getElementById('approveBtn');
  const rejectBtn = document.getElementById('rejectBtn');
  approveBtn.style.display = hasReport ? 'inline-block' : 'none';
  rejectBtn.style.display = hasReport ? 'inline-block' : 'none';
  approveBtn.disabled = false;
  rejectBtn.disabled = false;
}

async function decide(action) {
  if (!currentRunId) return;
  const r = await fetch('/api/' + action + '/' + currentRunId, {method: 'POST'});
  const data = await r.json();
  const status = document.getElementById('status');
  if (!r.ok) { status.textContent = 'Error: ' + (data.detail || 'failed'); return; }
  status.textContent = action === 'approve'
    ? 'Posted to Jira (comment ' + data.comment_id + ').'
    : 'Discarded — nothing posted.';
  document.getElementById('approveBtn').disabled = true;
  document.getElementById('rejectBtn').disabled = true;
}
</script>
</body>
</html>
"""
