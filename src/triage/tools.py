"""The agent's tool surface.

Design rules:

1. Narrow tools beat one wide tool. `nr_find_errors` beats "run any NRQL" for
   the 80% case because the query shape is already correct, so the model can't
   get it subtly wrong. The escape hatches (`nr_query`, `db_query`) exist for
   the 20%, guarded.
2. Every tool is read-only and declares `readOnlyHint=True`.
3. Every tool result is redacted and size-capped before it returns.
4. Every call is appended to the run trace, so the Jira comment can cite
   exactly which query produced which claim.

Code reading is NOT implemented here — the Agent SDK's built-in Read, Grep and
Glob tools already do it better than a custom wrapper, and they understand line
ranges and context. We just point `cwd`/`add_dirs` at the repos.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from .clients.databricks import DatabricksClient, SqlRejected
from .clients.github import GithubClient, GithubRequestError
from .clients.jira import JiraClient
from .clients.newrelic import NewRelicClient, NrqlRejected
from .config import Settings
from .redact import Redactor
from .trace import RunTrace

MAX_CHARS = 20_000

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _text(payload: str, is_error: bool = False) -> dict[str, Any]:
    body = payload if len(payload) <= MAX_CHARS else (
        payload[:MAX_CHARS] + f"\n\n... [truncated at {MAX_CHARS} chars — "
        "narrow the query with a tighter time window, more WHERE clauses, "
        "or fewer columns]"
    )
    out: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        out["isError"] = True
    return out


def build_tools(settings: Settings, redactor: Redactor, trace: RunTrace):
    """Returns (mcp_server_config, allowed_tool_names)."""

    nr = NewRelicClient(settings.newrelic)
    db = DatabricksClient(settings.databricks)
    jira = JiraClient(settings.jira)
    gh = GithubClient(settings.github) if settings.github else None
    pb = settings.playbook

    def _github_repo(service: str) -> str:
        repo = (pb.services.get(service) or {}).get("github_repo")
        if not repo:
            raise ValueError(
                f"No github_repo configured for service {service!r} in the playbook."
            )
        return repo

    # ---------------------------------------------------------------- New Relic

    @tool(
        "nr_query",
        "Run a read-only NRQL query against New Relic. Use for logs (FROM Log), "
        "errors (FROM TransactionError), and traces (FROM Span). ALWAYS include a "
        "SINCE clause; a LIMIT is added if you omit one. Start broad (count by "
        "error class), then drill into individual log lines.",
        {"nrql": str, "purpose": str},
        annotations=_READ_ONLY,
    )
    async def nr_query(args: dict[str, Any]) -> dict[str, Any]:
        try:
            res = nr.nrql(args["nrql"])
        except (NrqlRejected, RuntimeError) as e:
            trace.add("nr_query", args, error=str(e))
            return _text(f"Query rejected or failed: {e}", is_error=True)
        body = redactor.scrub(json.dumps(res.results, indent=2, default=str))
        trace.add(
            "nr_query",
            {"nrql": res.query, "purpose": args.get("purpose")},
            result_summary=f"{len(res.results)} rows",
            evidence_link=res.permalink,
        )
        notes = f"\n\nNRQL run: {res.query}\nPermalink: {res.permalink}"
        if res.messages:
            notes += f"\nNR messages: {res.messages}"
        return _text(body + notes)

    @tool(
        "nr_find_errors",
        "Pre-built error hunt: groups errors by class and message for a service "
        "over a window, so you learn the dominant failure mode in one call. "
        "Prefer this as your FIRST New Relic call.",
        {"service": str, "window": str},
        annotations=_READ_ONLY,
    )
    async def nr_find_errors(args: dict[str, Any]) -> dict[str, Any]:
        q = (
            "SELECT count(*), latest(error.message), latest(trace.id) "
            "FROM TransactionError, Log "
            f"WHERE appName = '{args['service']}' OR service.name = '{args['service']}' "
            "FACET error.class, level "
            f"{args['window']} LIMIT 50"
        )
        return await nr_query({"nrql": q, "purpose": "dominant failure mode"})

    @tool(
        "nr_trace",
        "Fetch every span and log line sharing a trace.id, ordered in time. This "
        "is how you turn 'something failed' into 'this call, to this dependency, "
        "at this millisecond'.",
        {"trace_id": str, "window": str},
        annotations=_READ_ONLY,
    )
    async def nr_trace(args: dict[str, Any]) -> dict[str, Any]:
        tid = args["trace_id"]
        spans = await nr_query(
            {
                "nrql": (
                    "SELECT timestamp, name, service.name, duration.ms, "
                    "otel.status_code, error.message FROM Span "
                    f"WHERE trace.id = '{tid}' {args['window']} "
                    "ORDER BY timestamp LIMIT 200"
                ),
                "purpose": f"span waterfall for trace {tid}",
            }
        )
        logs = await nr_query(
            {
                "nrql": (
                    "SELECT timestamp, level, message, service.name FROM Log "
                    f"WHERE trace.id = '{tid}' {args['window']} "
                    "ORDER BY timestamp LIMIT 200"
                ),
                "purpose": f"correlated logs for trace {tid}",
            }
        )
        merged = (
            "### Spans\n"
            + spans["content"][0]["text"]
            + "\n\n### Correlated logs\n"
            + logs["content"][0]["text"]
        )
        return _text(merged)

    # ---------------------------------------------------------------- Databricks

    @tool(
        "db_catalog",
        "List the tables you are allowed to read, with their purpose and key "
        "columns. Call this before writing any SQL — do not guess table names.",
        {},
        annotations=_READ_ONLY,
    )
    async def db_catalog(args: dict[str, Any]) -> dict[str, Any]:
        lines = ["Curated read-only tables:"]
        for name, meta in (pb.sql_templates or {}).items():
            lines.append(f"\n- template `{name}`: {meta.get('description', '')}")
            lines.append(f"  params: {list((meta.get('parameters') or {}).keys())}")
        lines.append("\nAd-hoc SELECTs are permitted on these tables only:")
        for t in (pb.services.get("_tables", {}) or {}).get("allow", []):
            lines.append(f"  {t}")
        return _text("\n".join(lines))

    @tool(
        "db_template",
        "Run a named, parameterised query from the playbook. Safest way to pull "
        "records for an entity you extracted from the ticket. Pass params as a "
        "JSON object of name -> value.",
        {"name": str, "params": str},
        annotations=_READ_ONLY,
    )
    async def db_template(args: dict[str, Any]) -> dict[str, Any]:
        tpl = (pb.sql_templates or {}).get(args["name"])
        if not tpl:
            return _text(
                f"No template named {args['name']!r}. Available: "
                f"{list((pb.sql_templates or {}).keys())}",
                is_error=True,
            )
        try:
            params = json.loads(args.get("params") or "{}")
        except json.JSONDecodeError as e:
            return _text(f"params is not valid JSON: {e}", is_error=True)
        missing = set(tpl.get("parameters", {})) - set(params)
        if missing:
            return _text(f"Missing parameters: {sorted(missing)}", is_error=True)
        try:
            res = db.query(tpl["sql"], parameters=params)
        except (SqlRejected, RuntimeError, TimeoutError) as e:
            trace.add("db_template", args, error=str(e))
            return _text(f"Query failed: {e}", is_error=True)
        trace.add(
            "db_template",
            {"name": args["name"], "params": params},
            result_summary=f"{res.row_count} rows",
            evidence_link=f"{settings.databricks.host}/sql/history?statementId="
            f"{res.statement_id}",
        )
        return _text(redactor.scrub(res.as_markdown()))

    @tool(
        "db_query",
        "Escape hatch: run an ad-hoc read-only SELECT. Use named parameter "
        "markers (:order_id) and pass values in `params` — never interpolate "
        "values from the ticket into the SQL string. Prefer db_template.",
        {"sql": str, "params": str, "purpose": str},
        annotations=_READ_ONLY,
    )
    async def db_query(args: dict[str, Any]) -> dict[str, Any]:
        try:
            params = json.loads(args.get("params") or "{}")
            res = db.query(args["sql"], parameters=params or None)
        except (SqlRejected, RuntimeError, TimeoutError, json.JSONDecodeError) as e:
            trace.add("db_query", args, error=str(e))
            return _text(f"Query rejected or failed: {e}", is_error=True)
        trace.add(
            "db_query",
            {"sql": res.statement, "params": params, "purpose": args.get("purpose")},
            result_summary=f"{res.row_count} rows",
            evidence_link=f"{settings.databricks.host}/sql/history?statementId="
            f"{res.statement_id}",
        )
        return _text(redactor.scrub(res.as_markdown()))

    # ---------------------------------------------------------------------- GitHub
    # Only registered when GITHUB_TOKEN is configured. Lets the agent read code,
    # history and PR context straight from GitHub — no local clone required.

    if gh is not None:

        @tool(
            "gh_file",
            "Read a file straight from GitHub, no local clone needed. Always reads "
            "the service's default branch (main/master/whatever it resolves to).",
            {"service": str, "path": str},
            annotations=_READ_ONLY,
        )
        async def gh_file(args: dict[str, Any]) -> dict[str, Any]:
            try:
                repo = _github_repo(args["service"])
                f = gh.get_file(repo, args["path"])
            except (ValueError, GithubRequestError) as e:
                trace.add("gh_file", args, error=str(e))
                return _text(str(e), is_error=True)
            trace.add(
                "gh_file", args,
                result_summary=f"{len(f.content)} chars ({f.ref})", evidence_link=f.html_url,
            )
            return _text(redactor.scrub(f.content))

        @tool(
            "gh_file_history",
            "List the commits that touched a file, most recent first — the fastest "
            "way to see what changed and when, equivalent to `git log -L` without a "
            "local clone.",
            {"service": str, "path": str, "limit": str},
            annotations=_READ_ONLY,
        )
        async def gh_file_history(args: dict[str, Any]) -> dict[str, Any]:
            try:
                repo = _github_repo(args["service"])
                commits = gh.commits_for_path(
                    repo, args["path"], limit=int(args.get("limit") or 20)
                )
            except (ValueError, GithubRequestError) as e:
                trace.add("gh_file_history", args, error=str(e))
                return _text(str(e), is_error=True)
            trace.add("gh_file_history", args, result_summary=f"{len(commits)} commits")
            if not commits:
                return _text("No commits found for that path.")
            lines = [
                f"{c.date}  {c.sha[:8]}  {c.author}: {redactor.scrub(c.message)}  ({c.url})"
                for c in commits
            ]
            return _text("\n".join(lines))

        @tool(
            "gh_blame",
            "Line-level blame on the default branch: which commit last touched each "
            "range of lines in a file. The single highest-value follow-up once "
            "you've found the suspect line in a log or stack trace.",
            {"service": str, "path": str},
            annotations=_READ_ONLY,
        )
        async def gh_blame(args: dict[str, Any]) -> dict[str, Any]:
            try:
                repo = _github_repo(args["service"])
                ranges = gh.blame(repo, args["path"])
            except (ValueError, GithubRequestError) as e:
                trace.add("gh_blame", args, error=str(e))
                return _text(str(e), is_error=True)
            trace.add("gh_blame", args, result_summary=f"{len(ranges)} ranges")
            if not ranges:
                return _text("No blame data returned for that path/ref.")
            lines = [
                f"L{r['startingLine']}-{r['endingLine']}  {r['commit']['oid'][:8]}  "
                f"{r['commit']['author']}  {r['commit']['committedDate']}: "
                f"{redactor.scrub(r['commit']['message'])}"
                for r in ranges
            ]
            return _text("\n".join(lines))

        @tool(
            "gh_pr_for_commit",
            "Find the pull request(s) a commit landed through — surfaces review "
            "discussion and intent that the bare commit message doesn't carry.",
            {"service": str, "sha": str},
            annotations=_READ_ONLY,
        )
        async def gh_pr_for_commit(args: dict[str, Any]) -> dict[str, Any]:
            try:
                repo = _github_repo(args["service"])
                prs = gh.pulls_for_commit(repo, args["sha"])
            except (ValueError, GithubRequestError) as e:
                trace.add("gh_pr_for_commit", args, error=str(e))
                return _text(str(e), is_error=True)
            trace.add("gh_pr_for_commit", args, result_summary=f"{len(prs)} PRs")
            if not prs:
                return _text("No pull request found for that commit.")
            out = [
                f"#{p['number']} {p['title']} ({p['html_url']})\n"
                + redactor.scrub((p.get('body') or '')[:800])
                for p in prs
            ]
            return _text("\n\n".join(out))

        @tool(
            "gh_search_code",
            "Search code in a service's repo by keyword — good for finding where a "
            "log message string or config key is defined when you don't know the "
            "file.",
            {"service": str, "query": str},
            annotations=_READ_ONLY,
        )
        async def gh_search_code(args: dict[str, Any]) -> dict[str, Any]:
            try:
                repo = _github_repo(args["service"])
                items = gh.search_code(repo, args["query"])
            except (ValueError, GithubRequestError) as e:
                trace.add("gh_search_code", args, error=str(e))
                return _text(str(e), is_error=True)
            trace.add("gh_search_code", args, result_summary=f"{len(items)} matches")
            if not items:
                return _text("No matches.")
            return _text("\n".join(f"{i['path']} ({i['html_url']})" for i in items))

    # ---------------------------------------------------------------------- Jira

    @tool(
        "jira_related_tickets",
        "Search Jira for tickets with a similar signature. If this bug has been "
        "seen before, the previous root cause is the cheapest evidence available.",
        {"text": str},
        annotations=_READ_ONLY,
    )
    async def jira_related_tickets(args: dict[str, Any]) -> dict[str, Any]:
        terms = args["text"].replace('"', " ")[:200]
        jql = (
            f'project = {settings.jira.project_key} AND text ~ "{terms}" '
            "ORDER BY created DESC"
        )
        try:
            keys = jira.search(jql, limit=10)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return _text(f"Jira search failed: {e}", is_error=True)
        trace.add("jira_related_tickets", {"jql": jql}, f"{len(keys)} matches")
        if not keys:
            return _text("No similar tickets found.")
        out = []
        for k in keys[:5]:
            issue = jira.get_issue(k)
            resolution = issue.comments[-1]["body"][:600] if issue.comments else ""
            out.append(
                f"### {k} [{issue.status}] {issue.summary}\n"
                f"{redactor.scrub(resolution)}"
            )
        return _text("\n\n".join(out))

    tools = [
        nr_query,
        nr_find_errors,
        nr_trace,
        db_catalog,
        db_template,
        db_query,
        jira_related_tickets,
    ]
    names = [
        "mcp__triage__nr_query",
        "mcp__triage__nr_find_errors",
        "mcp__triage__nr_trace",
        "mcp__triage__db_catalog",
        "mcp__triage__db_template",
        "mcp__triage__db_query",
        "mcp__triage__jira_related_tickets",
    ]

    if gh is not None:
        tools += [gh_file, gh_file_history, gh_blame, gh_pr_for_commit, gh_search_code]
        names += [
            "mcp__triage__gh_file",
            "mcp__triage__gh_file_history",
            "mcp__triage__gh_blame",
            "mcp__triage__gh_pr_for_commit",
            "mcp__triage__gh_search_code",
        ]

    # Read/Grep/Glob only make sense against an actual local clone (cwd/add_dirs
    # come from repo_path). No repo_path configured -> omit them so the model
    # isn't offered built-ins with nothing to point at; it falls back to gh_*.
    if pb.repo_paths():
        names += ["Read", "Grep", "Glob"]

    server = create_sdk_mcp_server(name="triage", version="1.0.0", tools=tools)
    return server, names
