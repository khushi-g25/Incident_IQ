# EPS/SQ Triage Agent

Ticket in, evidence-backed root cause out.

Reads a Jira EPS/SQ ticket, then correlates three sources to explain it:

| Source           | Answers                                                     | Access                                         |
| ---------------- | ----------------------------------------------------------- | ---------------------------------------------- |
| New Relic (NRQL) | **What** broke — exception class, log line, trace waterfall | NerdGraph GraphQL, read-only                   |
| Databricks SQL   | **Who and how many** — the records, and the blast radius    | Statement Execution API, SELECT-only           |
| Source code      | **Why** — the branch that was taken                         | Agent SDK `Read`/`Grep`/`Glob` on local clones |

It never mutates anything. The output is a structured report posted as a Jira
comment for a human to accept, correct, or reject.

> **Just want to run it?** See **[RUNNING.md](RUNNING.md)** — setup, commands,
> reading the output, and troubleshooting.
> **Want the structure?** See **[ARCHITECTURE.md](ARCHITECTURE.md)**.
> This README covers the design reasoning behind it.

---

## Architecture

![Incident IQ architecture](docs/architecture.png)

Full diagrams — module map, phase-by-phase flow, trust boundaries — are in
**[ARCHITECTURE.md](ARCHITECTURE.md)**. The sketch below is the text version.

```
Jira webhook / CLI
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 1. INTAKE (deterministic — no model)                    │
│    fetch issue + log attachments                        │
│    extract.py: IDs, timestamps, exceptions, stack frames│
│    → TicketBrief                                        │
└───────────────────────┬─────────────────────────────────┘
                        ▼
              ┌──────────────────┐   not triageable
              │ 2. GATE          ├──────────────► comment asking
              │ is_triageable?   │                for detail, exit
              └────────┬─────────┘
                       ▼  redact.py scrubs PII
┌─────────────────────────────────────────────────────────┐
│ 3. AGENT LOOP  (Claude Agent SDK)                       │
│                                                         │
│   tools.py (in-process MCP)     built-ins               │
│   ├ nr_apps / nr_event_types    ├ Grep                  │
│   │   / nr_attributes  (discover├ Read                  │
│   │   before you filter)        └ Glob                  │
│   ├ nr_find_errors               (only when a repo is   │
│   ├ nr_query                      cloned locally)       │
│   ├ nr_logs  (probes which                              │
│   │   attribute identifies logs)                        │
│   ├ nr_trace                                            │
│   ├ db_catalog / db_template / db_query  (disabled)     │
│   ├ jira_related_tickets                                │
│   └ gh_file / gh_file_history / gh_blame /               │
│     gh_pr_for_commit / gh_search_code (optional)        │
│                                                         │
│   guardrails.py PreToolUse hook denies every write,     │
│   path escape, and over-budget call                     │
│   trace.py records every call + a deep link             │
└───────────────────────┬─────────────────────────────────┘
                        ▼  output_format: json_schema
┌─────────────────────────────────────────────────────────┐
│ 4. HAND OFF                                             │
│    schema.py renders report → markdown → ADF → Jira     │
│    trace written to .runs/EPS-1234-<run_id>.json        │
└─────────────────────────────────────────────────────────┘
```

### Why the Agent SDK rather than a hand-rolled tool loop

You get the agent loop, context compaction, and — critically — `Read`, `Grep`
and `Glob` for free. That is the entire "read the code" leg of your problem,
done better than a custom wrapper, with line ranges and context windows already
handled. You only write the two tools that don't exist: New Relic and Databricks.

### Layout

```
src/triage/
  config.py          settings from env + playbook YAML loader
  extract.py         deterministic entity/stack-trace extraction
  redact.py          PII pseudonymisation (stable placeholders)
  tools.py           the agent's tool surface (in-process MCP server)
  guardrails.py      PreToolUse hooks: no writes, no path escape, budget cap
  trace.py           append-only audit log → Jira evidence table
  schema.py          report schema, confidence validator, markdown renderer
  prevention.py      builds the prevention document from the report
  simplify.py        tool-free second pass: plain prose for the UI
  agent.py           orchestration
  cli.py             `triage EPS-1234`
  clients/
    jira.py          read via API v2, comment via API v3 (ADF)
    newrelic.py      NerdGraph NRQL + query validation
    databricks.py    Statement Execution API + SQL validation
    github.py        REST (file/commits/PRs/search) + GraphQL blame, optional
config/playbooks/
  eps.yaml           ← your domain knowledge lives here
prompts/system.md    the triage method
service/ui.py        local approval UI (run, review, approve)
service/webhook.py   Jira webhook → background triage
tests/test_safety.py guardrails, NRQL repair, report rendering, ADF
tests/test_ui.py     the approve/reject path
```

---

## Integration notes

The things that will cost you an afternoon each if you don't know them.

### Jira

- Auth is Basic with `email:api_token` base64'd — **not** a bearer token. Tokens
  come from `id.atlassian.com/manage-profile/security/api-tokens`.
- Read through **API v2**, not v3. v3 returns the description as an Atlassian
  Document Format tree you'd have to walk; v2 returns plain wiki markup that
  goes straight into a prompt.
- Write through **API v3** — comments must be ADF, and ADF has no markdown
  parser, so anything not converted arrives as literal punctuation.
  `clients/jira.py::_to_adf` handles headings, bold/italic/inline-code/links,
  bullet and ordered lists, fenced code, rules, pipe tables, and task lists
  (which render as real tickable checkboxes).
- A comment over **32,767 characters** is rejected with
  `CONTENT_LIMIT_EXCEEDED` and *nothing* is posted. `render_markdown` fits the
  body to a budget measured against ADF, which serialises to roughly 1.8x the
  markdown, and sacrifices the evidence appendix before the diagnosis.
- Attached `.log`/`.txt` files are usually the richest signal in the whole
  ticket. Fetch them via the attachment `content` URL with the same auth.

### New Relic

- There is **no REST API for logs**. Everything goes through NRQL over the
  NerdGraph GraphQL endpoint at `api.newrelic.com/graphql` (`api.eu.` for EU).
- Header is `API-Key`, and it must be a **User key** (`NRAK-…`). A licence or
  ingest key will authenticate and then return nothing useful.
- The NRQL string is passed as a GraphQL variable of the custom scalar type
  `Nrql!`. Don't string-build the GraphQL document.
- Always bound with `SINCE`. A single result set caps at 5000 rows, and
  unbounded windows are slow and expensive. `prepare()` injects a window and
  a `LIMIT` if the model forgets.
- The join key across all of this is `trace.id`. Once the agent has one,
  `nr_trace` pulls the span waterfall _and_ the correlated log lines, which is
  what turns "it failed" into "this dependency call timed out at this ms".
- **Discover before you filter.** `appName` is the single biggest source of
  false negatives: repo name, service name and NR app name are all different,
  and a guessed `appName` returns zero rows that are indistinguishable from
  "nothing is broken". `nr_apps`, `nr_event_types` and `nr_attributes` exist so
  the agent reads the real names out of the account instead of inventing them,
  and `_run_nrql` appends an explicit warning to every empty result set.
- **NRQL is not SQL.** `SELECT count(*), error.class FROM TransactionError`
  fails with "Value must be constant in its context" — an aggregate and a bare
  attribute cannot coexist in a SELECT. Models make this mistake constantly, so
  `prepare()` repairs it by moving the attribute into `FACET` (before the
  window/limit clauses, where it must go) and tells the agent what it changed.
- The mutation guard matches keywords against a **literal-stripped** copy of the
  query. Matching them raw rejected any query touching an endpoint named
  `.../create` or `.../update`, which is most of a latency investigation.

### Databricks

- Use the **Statement Execution API** (`POST /api/2.0/sql/statements`), not
  `databricks-sql-connector`. It's plain HTTP, no Thrift/ODBC driver to ship,
  and the async poll model (`wait_timeout: 30s` + `on_wait_timeout: CONTINUE`,
  then `GET /api/2.0/sql/statements/{id}`) survives a slow query without
  holding a socket open.
- Results come back as `result.data_array` (positional) with column names in
  `manifest.schema.columns` — you have to zip them yourself.
- **Use `:named` parameter markers.** The SQL text is model-authored and the
  values come from a ticket that anyone can file. `db_template` and `db_query`
  both pass values via the `parameters` array; nothing is concatenated.
- The token belongs to a **service principal with SELECT-only grants** on a
  curated table list. The in-process SQL validator is defence in depth, not
  your actual security boundary. If the agent can `DROP`, you have a grants
  problem, not a prompt problem.

### Code

- Clone the repos to a known path and point the playbook at them. Set `cwd` to
  the primary repo and pass the rest via `add_dirs`.
- The fastest route from log line to source line is grepping for the **literal
  log message string**, not the exception class. The system prompt says so.
- `git log -L` on the identified line is the highest-value follow-up, but Bash
  is denied by the guardrail. `clients/github.py` + the `gh_*` tools below are
  that narrow, read-only escape hatch.

### GitHub (optional — code access without a local clone)

- Set `GITHUB_TOKEN` (a fine-grained PAT with **Contents: Read-only** and
  **Pull requests: Read-only** on the repos in scope) and add a `github_repo:
owner/repo` key to each service in the playbook. Unset means the `gh_*`
  tools simply aren't registered — `Read`/`Grep`/`Glob` on local clones keep
  working exactly as before.

#### Getting a token, step by step

1. Log in to **github.com** as an account that has (or can request) access to
   the repos you want the agent to read.
2. Click your profile picture, top-right → **Settings**.
3. Scroll to the very bottom of the left-hand sidebar → **Developer settings**.
4. In the left sidebar → **Personal access tokens** → **Fine-grained tokens**.
5. Click **Generate new token** (top-right).
6. Fill in the form:
   - **Token name** — something identifiable, e.g. `triage-agent-eps`.
   - **Expiration** — pick a real date (max 1 year); you'll need to rotate it
     when it lapses.
   - **Resource owner** — the user or organisation that owns the repos (if it's
     an org, the org must allow fine-grained tokens under
     _Org Settings → Personal access tokens_).
   - **Repository access** → **Only select repositories** → pick every repo
     listed as a `github_repo:` value in `config/playbooks/eps.yaml`.
7. Under **Permissions → Repository permissions**, set:
   - **Contents** → `Read-only`
   - **Pull requests** → `Read-only`
   - (Metadata: Read-only is added automatically — leave it.)
     Leave every other permission at **No access**.
8. Click **Generate token**.
9. **Copy the token immediately** — it's shown once, in a box starting with
   `github_pat_`. If you navigate away before copying it, you must generate a
   new one.
10. If the resource owner is an organisation with approval required, the token
    stays **Pending** until an org admin approves it under
    _Org Settings → Personal access tokens → Pending requests_. It won't
    authenticate until then.
11. Paste the copied value into your `.env` file (create it from
    `.env.example` if you haven't already):
    ```
    GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxxxxxx
    ```
12. Save `.env`. Never commit it — it's already listed in `.gitignore`.
13. Restart the CLI/webhook process so it picks up the new env var. Verify
    quickly with:
    ```bash
    curl -sS -H "Authorization: Bearer $GITHUB_TOKEN" \
         -H "Accept: application/vnd.github+json" \
         https://api.github.com/repos/<owner>/<repo>
    ```
    A `200` with repo JSON means the token and permissions are wired up
    correctly; a `404`/`403` means either the repo wasn't selected in step 6,
    the permission in step 7 is missing, or the token is still pending
    approval (step 10).

- `clients/github.py` has **no write methods at all** — that's the actual
  security boundary, same as the SELECT-only Databricks principal.
- Auth is `Authorization: Bearer <token>` plus `X-GitHub-Api-Version`, not
  Basic auth like Jira.
- Every read pins to the repo's **default branch**, resolved once via
  `GET /repos/{owner}/{repo}` and cached — there's no `ref`/`tag`/`sha`
  parameter for a tool call to wander off with.
- Five tools, matched to `nr_query`/`nr_find_errors` narrow-beats-wide
  philosophy:
  - `gh_file` — read a file from the default branch without cloning.
  - `gh_file_history` — commits touching a file on the default branch, i.e.
    `git log -L` without a clone.
  - `gh_blame` — **line-level** blame on the default branch. This is GraphQL,
    not REST — GitHub's REST API has no blame endpoint at all.
  - `gh_pr_for_commit` — the PR (and its review discussion) a commit landed
    through; often has the _why_ a commit message doesn't.
  - `gh_search_code` — find where a log string or config key is defined when
    you don't know the file.
- If you already clone repos locally for `Read`/`Grep`/`Glob`, you mostly want
  this for `gh_blame` and `gh_pr_for_commit` — the two things a local clone
  either can't do well (blame needs `git blame`, slower and noisier to parse
  from Bash) or can't do at all (a commit doesn't know which PR it shipped in
  without hitting GitHub's API anyway).

### A shortcut worth knowing

Your workspace already has Atlassian and Databricks MCP connectors. Before
building any of this, prototype in Claude Code or Cowork: point it at a repo,
connect those two, paste a ticket, and watch what a human-driven session
actually needs. A week of that will teach you more about your tool surface and
your playbook contents than a month of design. Build this repo when you want it
to run unattended, on every ticket, with an audit trail.

New Relic has no first-party connector in that set, which is why
`clients/newrelic.py` exists either way.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # fill it in
pytest                  # guardrails must pass before you point this at prod

# dry run — prints the report, posts nothing
triage EPS-1234

# post the comment
triage EPS-1234 --post

# hard ticket
triage EPS-1234 --model opus
```

Webhook mode, once you trust it:

```bash
uvicorn service.webhook:app --port 8080
# Jira → System → Webhooks → https://host/webhook/jira?secret=...
# fires on issue_created + issue_updated, scoped by JQL to project = EPS
```

Local approve UI — paste a ticket, get a plain-English summary, post to Jira
only if you click Approve:

```bash
uvicorn service.ui:app --port 8090 --reload
# open http://127.0.0.1:8090
```

Keep `--reload` on while you are tuning. `prompts/system.md`, `REPORT_SCHEMA`
and the tool definitions are all read at import time, so a server started
before an edit keeps generating reports in the old shape — which looks exactly
like the prompt change having no effect.

`service/ui.py` always runs `triage(..., post=False)` regardless of
`TRIAGE_DRY_RUN` — the agent never posts on its own here. `POST /api/approve`
is the only thing that calls `JiraClient.add_comment`, and it does so
unconditionally once you click it, so treat Approve as final.

---

## The playbook is the product

`config/playbooks/eps.yaml` is where the leverage is. The Python is generic; the
playbook is what makes the agent good at _your_ tickets. Four sections:

- **`services`** — ticket vocabulary → repo path + New Relic app name. `aliases`
  catches what humans actually type ("the register", "ordersvc").
- **`entity_patterns`** — regexes for your identifiers. Extraction is
  deterministic, so the model never burns turns parsing. Use `\W{0,3}` for
  separators, never `\D{0,3}` — `\D` matches letters and will silently eat the
  first characters of an alphanumeric ID.
- **`sql_templates`** — the safe parameterised path. Rule of thumb: when the
  agent writes the same ad-hoc query twice, it becomes a template.
- **`triage_notes`** — tribal knowledge, injected into the prompt. The
  known-flaky dependency, the misleading log line, the retry that swallows
  errors, the status field that doesn't mean what it says. Highest-value field
  in the file, and the one that most improves accuracy per line written.

---

## Rollout

**Week 1 — offline eval.** Pick 30 closed EPS tickets whose root cause you
already know. Run against all of them with `TRIAGE_DRY_RUN=1`. Score three
things separately: did it find the right component, did it cite real evidence,
and did it hallucinate a cause. That last number is the one that decides whether
this ships. Verdict distribution matters too — an agent that says
`root_cause_identified` on every ticket is miscalibrated, not good.

**Week 2 — playbook iteration.** Read the traces, not just the reports. Every
wrong turn is usually a missing `triage_note`, a missing `sql_template`, or a
service alias you didn't know humans used.

**Week 3 — shadow mode.** Webhook on, gated by the `auto-triage` label, posting
real comments on a subset. Engineers thumbs-up/down each one. Keep it labelled
until the false-cause rate is low enough that reading the comment is faster than
ignoring it.

**Week 4+ — widen.** Drop the label gate per ticket type, not all at once.

Things to keep true as you widen:

- The agent diagnoses; it never fixes, never transitions a ticket, never writes
  to a warehouse. A wrong comment costs a minute. A wrong `UPDATE` costs a day.
- Every claim carries the query that produced it. Unaudited conclusions during
  an incident are worse than no conclusions.
- Budgets are enforced in three places (`max_turns`, `max_budget_usd`, the
  hook's tool-call cap) because a confused agent loops, and Databricks queries
  are not free.
- `narrowed_not_confirmed` with three solid facts is a _good_ outcome. Tune the
  prompt toward honest partial answers; the failure mode you actually fear is a
  confident wrong root cause that sends an engineer down the wrong path at 2am.
- Confidence is not taken on trust. The model declares which of the three
  signals (logs / code / data) it actually confirmed in `signals_confirmed`,
  and `validate_report()` caps the stated confidence against that and against
  the run's real hit rate — a run where every query came back empty cannot
  publish a high-confidence cause. Downgrades are printed in the comment, not
  applied silently, so the reader sees why.
- The comment is read by two audiences. `plain_language` is rendered first, in
  business terms with no file paths or class names, for the support lead or PM
  deciding whether to escalate; `Technical detail` follows for the engineer.
  Verdict and confidence enums are translated to prose on the way out —
  nobody outside the team should have to decode `narrowed_not_confirmed`.
