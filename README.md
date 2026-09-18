# EPS/SQ Triage Agent

Ticket in, evidence-backed root cause out.

Reads a Jira EPS/SQ ticket, then correlates three sources to explain it:

| Source | Answers | Access |
|---|---|---|
| New Relic (NRQL) | **What** broke — exception class, log line, trace waterfall | NerdGraph GraphQL, read-only |
| Databricks SQL | **Who and how many** — the records, and the blast radius | Statement Execution API, SELECT-only |
| Source code | **Why** — the branch that was taken | Agent SDK `Read`/`Grep`/`Glob` on local clones |

It never mutates anything. The output is a structured report posted as a Jira
comment for a human to accept, correct, or reject.

---

## Architecture

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
│   ├ nr_find_errors              ├ Grep                  │
│   ├ nr_query                    ├ Read                  │
│   ├ nr_trace                    └ Glob                  │
│   ├ db_catalog / db_template / db_query                 │
│   └ jira_related_tickets                                │
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
  schema.py          report JSON schema + markdown renderer
  agent.py           orchestration
  cli.py             `triage EPS-1234`
  clients/
    jira.py          read via API v2, comment via API v3 (ADF)
    newrelic.py      NerdGraph NRQL + query validation
    databricks.py    Statement Execution API + SQL validation
config/playbooks/
  eps.yaml           ← your domain knowledge lives here
prompts/system.md    the triage method
service/webhook.py   Jira webhook → background triage
tests/test_safety.py guardrails + extraction (the parts that must be exact)
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
- Write through **API v3** — comments must be ADF. `clients/jira.py::_to_adf`
  is a minimal Markdown→ADF converter (paragraphs + code blocks), which is all
  the report needs.
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
  unbounded windows are slow and expensive. `validate()` injects a window and
  a `LIMIT` if the model forgets.
- The join key across all of this is `trace.id`. Once the agent has one,
  `nr_trace` pulls the span waterfall *and* the correlated log lines, which is
  what turns "it failed" into "this dependency call timed out at this ms".

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
  is denied by the guardrail. If you want it, add a narrow `git_blame` tool
  rather than opening up Bash.

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

---

## The playbook is the product

`config/playbooks/eps.yaml` is where the leverage is. The Python is generic; the
playbook is what makes the agent good at *your* tickets. Four sections:

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
- `narrowed_not_confirmed` with three solid facts is a *good* outcome. Tune the
  prompt toward honest partial answers; the failure mode you actually fear is a
  confident wrong root cause that sends an engineer down the wrong path at 2am.
