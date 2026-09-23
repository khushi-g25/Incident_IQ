# Running Incident IQ

How to get this project running locally, triage a ticket, and check the output
before anything reaches Jira.

For *why* it is built this way — tool design, guardrails, the rollout plan —
see [README.md](README.md). This file is just operations.

---

## 1. What it does

You give it a Jira ticket key. It reads the ticket, searches New Relic for the
matching logs and errors, reads the relevant service code on GitHub, and writes
one comment containing a root cause, a recommended fix, and the queries that
back both claims.

It is read-only everywhere except the single Jira comment it posts, and it
posts nothing unless you explicitly ask it to.

```
Jira ticket ──► extract IDs/timestamps ──► gate ──► agent loop ──► report
                                                    (New Relic,     │
                                                     GitHub, Jira)  ▼
                                                           Jira comment
                                                           (only on request)
```

---

## 2. Prerequisites

| Need | Version | Notes |
|---|---|---|
| Python | 3.10+ | 3.13 is what this is developed on |
| Jira API token | — | [id.atlassian.com → Security → API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| New Relic **user** key | `NRAK-…` | A licence or ingest key authenticates and then returns nothing |
| Model access | — | Either an Anthropic API key **or** AWS Bedrock credentials |
| GitHub PAT | optional | Fine-grained, `Contents: Read` + `Pull requests: Read` |

**You do not need Node.js or a separate Claude Code install.** The
`claude-agent-sdk` package ships its own CLI binary at
`.venv/lib/python3.*/site-packages/claude_agent_sdk/_bundled/claude`, and the
SDK prefers it over anything on your `PATH`.

---

## 3. Setup

```bash
git clone <repo-url>
cd Incident_IQ

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
```

That installs the package in editable mode and puts a `triage` command on your
`PATH` (inside the venv).

Then create your config:

```bash
cp .env.example .env
```

Fill in `.env` — see the next section — and confirm the guardrails pass:

```bash
pytest -q
```

You should see **90 passed**. These are the tests that stop a model-authored
string from reaching production, so do not skip this step.

---

## 4. Configuration

Everything comes from `.env` plus one playbook YAML. Nothing is hardcoded.

### Model access — pick one

**Anthropic API:**

```bash
ANTHROPIC_API_KEY=sk-ant-...
TRIAGE_MODEL=sonnet              # or: opus
```

**AWS Bedrock:**

```bash
CLAUDE_CODE_USE_BEDROCK=1
AWS_REGION=us-east-1
AWS_BEARER_TOKEN_BEDROCK=...     # or standard AWS_ACCESS_KEY_ID / AWS_PROFILE
TRIAGE_MODEL=arn:aws:bedrock:us-east-1:<acct>:application-inference-profile/<id>
```

Bedrock turns on automatically when `AWS_BEARER_TOKEN_BEDROCK` is set and
`ANTHROPIC_API_KEY` is not. Set `CLAUDE_CODE_USE_BEDROCK` explicitly to remove
the ambiguity.

### Required for every run

```bash
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=svc-triage@yourcompany.com
JIRA_API_TOKEN=
JIRA_PROJECT_KEY=SQ                 # the project related-ticket search is scoped to

NEW_RELIC_API_KEY=NRAK-...          # USER key
NEW_RELIC_ACCOUNT_ID=1234567
NEW_RELIC_ENDPOINT=https://api.newrelic.com/graphql   # EU: api.eu.newrelic.com
```

### Optional

```bash
GITHUB_TOKEN=                       # unset = gh_* tools are not registered at all

TRIAGE_DRY_RUN=1                    # 1 = never write to Jira
TRIAGE_MAX_TURNS=60
TRIAGE_MAX_BUDGET_USD=2.00
TRIAGE_FALLBACK_MODEL=
TRIAGE_PLAYBOOK=config/playbooks/eps.yaml
```

### Budgets

A run stops at whichever of these is hit first:

- `TRIAGE_MAX_TURNS` (default 60)
- `TRIAGE_MAX_BUDGET_USD` (default $2.00)
- 60 tool calls, enforced by the `PreToolUse` hook in `src/triage/guardrails.py`

Typical run: **50–65 tool calls, $0.50–$0.80, two to four minutes.**

### The playbook

`config/playbooks/eps.yaml` holds the domain knowledge: which services exist,
their New Relic app names, their GitHub repos, the regexes for your domain IDs,
and the triage notes the agent is told to respect. This is the file to edit
when the agent keeps guessing wrong about your estate — not the prompt.

---

## 5. Running it

### CLI — use this while iterating

```bash
source .venv/bin/activate

# dry run: prints the comment to stdout, posts nothing
triage SQ-1794

# the raw structured report, for checking individual fields
triage SQ-1794 --json

# a harder ticket
triage SQ-1794 --model opus

# a different playbook
triage SQ-1794 --playbook config/playbooks/other.yaml

# actually post the comment to Jira
triage SQ-1794 --post
```

Progress is written to stderr as it goes, so you can watch which queries run:

```
[17:39:04] 🚀 starting triage for SQ-1794
[17:39:05] 📥 [intake] fetching Jira issue SQ-1794 ...
[17:39:07] ✅ [gate] ticket passed — proceeding to agent
[17:39:41] 🔧 [tool] nr_query (820ms) -> 102 rows
[17:41:12] 🏁 [agent] finished: cost=$0.5961
```

Each CLI invocation is a fresh process, so it always picks up your latest
prompt, schema and playbook edits.

### Web UI — use this for review and approval

```bash
source .venv/bin/activate
uvicorn service.ui:app --port 8090 --reload
# open http://127.0.0.1:8090
```

Paste a ticket link or key, optionally add extra context ("focus on the
payments service"), and click **Run triage**. You get:

- a plain-English summary
- a **Recommended fix** panel — the change, where to make it, how to confirm it
  worked, and the next actions
- the full comment exactly as it would be posted
- a live log of every query while it runs

Nothing goes to Jira until you click **Approve**. This path always runs with
`post=False` regardless of `TRIAGE_DRY_RUN`.

> **Keep `--reload` on.** The system prompt, the report schema and the tool
> definitions are all read at import time. A server started before an edit
> keeps producing reports in the old shape, which looks exactly like your
> change having had no effect. If output does not match your edits, check the
> server's start time before debugging anything else:
> `ps -eo pid,lstart,command | grep uvicorn`

### Webhook — unattended, once you trust it

```bash
uvicorn service.webhook:app --port 8080
```

Requires `TRIAGE_WEBHOOK_SECRET` to be set. Point Jira at
`https://<host>/webhook/jira?secret=...` (System → Webhooks), firing on
`issue_created` and `issue_updated`.

It only acts on tickets carrying the `TRIAGE_TRIGGER_LABEL` label
(default `auto-triage`), so you can roll it out to a labelled subset first.
`GET /healthz` is the liveness check.

---

## 6. Reading the output

Every run writes two artefacts:

- `.runs/prevention/<TICKET>-<run_id>.md` — the prevention document: whether
  this was a code bug, what to change so the class of failure goes away, how it
  would be detected next time, and a paste-able runbook entry. Written on every
  run, including the ones where nothing in the code was wrong.
- `.runs/<TICKET>-<run_id>.json` — the full trace: each tool
call, its arguments, the row count, and a New Relic permalink. When you
disagree with a conclusion, read the trace — it shows exactly what the agent
saw.

```bash
# what did it actually query, and what came back?
python3 -c "
import json; d=json.load(open('.runs/SQ-1794-0c35a34a16fc.json'))
print(d['turns'], 'calls  \$', round(d['cost_usd'],3))
for e in d['entries']:
    print(e['tool'], '->', e.get('result_summary') or e.get('error'))
"
```

### Verdicts

| Verdict | Means |
|---|---|
| `root_cause_identified` | Logs, data and code agree |
| `narrowed_not_confirmed` | Real facts, cause not yet proven — a good outcome |
| `not_reproducible_from_data` | Could not find the behaviour in telemetry |
| `insufficient_information` | Ticket lacks detail to investigate |
| `not_a_bug` | Working as designed |

Confidence is **not** taken on the model's word. It declares which signals
(logs / code / data) it actually confirmed, and `validate_report()` caps the
stated confidence against that and against how many queries returned rows. Any
downgrade is printed in the comment rather than applied silently, so a
"moderate confidence" note explaining itself is the system working.

`narrowed_not_confirmed` with three solid facts is more useful than a
confident wrong root cause. Tune toward honest partial answers.

---

## 7. Troubleshooting

**Approve does nothing / says "unknown run_id"**
Pending approvals are mirrored to `.runs/pending/`, so they now survive the
worker restart that `--reload` triggers on every file save. If Jira refuses the
post, the UI shows Jira's own reason and keeps the report so you can retry once
the cause is fixed.

**Output does not reflect my prompt or schema edits**
The UI server is holding old code. Restart it with `--reload`, or use the CLI,
which is always fresh. Check with `ps -eo pid,lstart,command | grep uvicorn`.

**`Missing required env var: X`**
`X` is absent from `.env`. Note that `.env` is read from the repo root, so run
commands from there.

**New Relic queries all return 0 rows**
Usually a wrong `appName`. Repo names, service names and New Relic app names
differ. The agent has `nr_apps`, `nr_event_types` and `nr_attributes` to
discover the real names — if it is not using them, say so in the extra-context
box. Also check you are using a **user** key, not a licence key.

**`Automated triage skipped — no error signature, identifier or timestamp`**
The gate rejected the ticket before spending money. It had nothing machine-
usable to correlate on. Add an error message, an ID or a timestamp to the
ticket, or extend `entity_patterns` in the playbook so your domain IDs are
recognised.

**GitHub tools return 404 on a repo that exists**
The fine-grained PAT has not had that repo selected, or the org needs a
separate SSO grant for the token. `csp-foundation` is commented out in the
playbook for this reason.

**Jira search fails / no related tickets ever found**
Atlassian retired `POST /rest/api/2/search`. The client tries
`/rest/api/3/search/jql` first and falls back, so this should be handled — if
it still fails, check the token has browse permission on the project.

**Run ends early, having found little**
It hit a budget. Look at the tail of the trace, then raise
`TRIAGE_MAX_TURNS` / `TRIAGE_MAX_BUDGET_USD`, or narrow the ticket's scope
using the extra-context box.

---

## 8. Before you let it post

Run it against 20–30 **already-resolved** tickets and compare its verdict to
the real root cause. Keep `TRIAGE_DRY_RUN=1` until the false-cause rate is low
enough that reading the comment is faster than ignoring it. Then widen by
ticket type, not all at once.

The agent diagnoses; it never fixes, never transitions a ticket, never writes
to a warehouse. A wrong comment costs a minute of someone's time — keep it
that way.
