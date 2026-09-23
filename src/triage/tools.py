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
import re
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

# from .clients.databricks import DatabricksClient, SqlRejected
from .clients.github import GithubClient, GithubRequestError
from .clients.jira import JiraClient
from .clients.newrelic import NewRelicClient, NrqlRejected
from .config import Settings
from .redact import Redactor
from .trace import RunTrace

MAX_CHARS = 20_000

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _esc(value: str) -> str:
    """Escape a value being interpolated into an NRQL string literal.

    Values reaching these tools come from a model reading a ticket, so an
    unescaped apostrophe is a routine occurrence, not an attack -- but it
    breaks the query either way.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


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


def build_tools(
    settings: Settings,
    redactor: Redactor,
    trace: RunTrace,
    default_window: str | None = None,
):
    """Returns (mcp_server_config, allowed_tool_names).

    `default_window` is the SINCE/UNTIL clause derived from the ticket. The
    discovery tools fall back to it so that "what app names exist?" is answered
    for the incident window rather than an arbitrary last-24-hours.
    """

    nr = NewRelicClient(settings.newrelic)
    # db = DatabricksClient(settings.databricks)
    jira = JiraClient(settings.jira)
    gh = GithubClient(settings.github) if settings.github else None
    pb = settings.playbook

    def pb_window() -> str:
        return default_window or settings.newrelic.default_window

    def _github_repo(service: str) -> str:
        repo = (pb.services.get(service) or {}).get("github_repo")
        if not repo:
            raise ValueError(
                f"No github_repo configured for service {service!r} in the playbook."
            )
        return repo

    # ---------------------------------------------------------------- New Relic
    #
    # `_run_nrql` is a plain function, deliberately. The @tool decorator returns
    # an SdkMcpTool dataclass, not a callable, so composite tools calling each
    # other through the decorated name raise TypeError at runtime -- which is
    # what silently disabled nr_find_errors and nr_trace on every run.

    async def _query(
        nrql: str, purpose: str
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        """Run once, return both the rows and the tool result.

        Tools that need to branch on the data (is there any log volume at all?)
        get the rows; tools that just hand the result to the model ignore them.
        `None` rows means the query failed, which is different from zero rows.
        """
        try:
            res = nr.nrql(nrql)
        except (NrqlRejected, RuntimeError) as e:
            trace.add("nr_query", {"nrql": nrql, "purpose": purpose}, error=str(e))
            return None, _text(f"Query rejected or failed: {e}", is_error=True)

        body = redactor.scrub(json.dumps(res.results, indent=2, default=str))
        trace.add(
            "nr_query",
            {"nrql": res.query, "purpose": purpose},
            result_summary=f"{len(res.results)} rows",
            evidence_link=res.permalink,
        )
        footer = [f"\n\nNRQL run: {res.query}", f"Permalink: {res.permalink}"]
        if res.notes:
            footer.append("Adjustments made to your query:")
            footer += [f"  - {n}" for n in res.notes]
        if res.messages:
            footer.append(f"NR messages: {res.messages}")
        if not res.results:
            footer.append(
                "ZERO ROWS. Before concluding 'no errors happened', rule out the "
                "boring explanations: wrong appName (run nr_apps), wrong event "
                "type (nr_event_types), wrong attribute name (nr_attributes), or "
                "a window in the wrong timezone. Absence of data is only evidence "
                "once you have confirmed you queried the right place."
            )
        return res.results, _text(body + "\n".join(footer))

    async def _run_nrql(nrql: str, purpose: str) -> dict[str, Any]:
        return (await _query(nrql, purpose))[1]

    @tool(
        "nr_query",
        "Run a read-only NRQL query against New Relic. Use for logs (FROM Log), "
        "errors (FROM TransactionError), and traces (FROM Span). ALWAYS include a "
        "SINCE clause; a LIMIT is added if you omit one. NRQL cannot return an "
        "aggregate and a bare attribute in the same SELECT -- put the attribute "
        "in FACET instead. Start broad (count by error class), then drill in.",
        {"nrql": str, "purpose": str},
        annotations=_READ_ONLY,
    )
    async def nr_query(args: dict[str, Any]) -> dict[str, Any]:
        return await _run_nrql(args["nrql"], args.get("purpose") or "")

    @tool(
        "nr_apps",
        "List the appName values New Relic has actually seen in a window, with "
        "event counts. Run this BEFORE filtering on any appName: repo names, "
        "service names and NR app names differ, and a guessed appName returns "
        "zero rows that look exactly like 'no errors'. Optional `contains` "
        "filters the list case-insensitively.",
        {"window": str, "contains": str},
        annotations=_READ_ONLY,
    )
    async def nr_apps(args: dict[str, Any]) -> dict[str, Any]:
        window = args.get("window") or pb_window()
        needle = (args.get("contains") or "").strip()
        where = f" WHERE appName LIKE '%{_esc(needle)}%'" if needle else ""
        out = []
        for event in ("Transaction", "TransactionError", "Log"):
            res = await _run_nrql(
                f"SELECT count(*) FROM {event}{where} {window} "
                "FACET appName LIMIT MAX",
                purpose=f"discover appName values reporting {event}",
            )
            out.append(f"### appName values seen in {event}\n" + res["content"][0]["text"])
        return _text("\n\n".join(out))

    @tool(
        "nr_event_types",
        "List the event types (data tables) that exist in this New Relic account "
        "for a window. Use it when a query returns zero rows and you need to know "
        "whether the event type you queried carries any data at all.",
        {"window": str},
        annotations=_READ_ONLY,
    )
    async def nr_event_types(args: dict[str, Any]) -> dict[str, Any]:
        return await _run_nrql(
            f"SHOW EVENT TYPES {args.get('window') or pb_window()}",
            purpose="discover available event types",
        )

    @tool(
        "nr_attributes",
        "List the attribute names available on an event type, optionally scoped "
        "to one appName. Use before filtering or faceting on an attribute you "
        "have not already seen in a result -- attribute naming varies per agent "
        "(error.class vs errorClass, name vs transactionName).",
        {"event_type": str, "app_name": str, "window": str},
        annotations=_READ_ONLY,
    )
    async def nr_attributes(args: dict[str, Any]) -> dict[str, Any]:
        app = (args.get("app_name") or "").strip()
        where = f" WHERE appName = '{_esc(app)}'" if app else ""
        return await _run_nrql(
            f"SELECT keyset() FROM {args['event_type']}{where} "
            f"{args.get('window') or pb_window()}",
            purpose=f"discover attributes on {args['event_type']}",
        )

    @tool(
        "nr_find_errors",
        "Pre-built error hunt: groups errors by class and message for one "
        "appName over a window, so you learn the dominant failure mode in one "
        "call. Pass an appName confirmed via nr_apps, not a repo name. Queries "
        "TransactionError and Log separately, because their attributes differ.",
        {"service": str, "window": str},
        annotations=_READ_ONLY,
    )
    async def nr_find_errors(args: dict[str, Any]) -> dict[str, Any]:
        app = _esc(args["service"])
        window = args.get("window") or pb_window()
        errors = await _run_nrql(
            "SELECT count(*), latest(error.message), latest(trace.id) "
            f"FROM TransactionError WHERE appName = '{app}' {window} "
            "FACET `error.class`, `transactionName` LIMIT 50",
            purpose=f"dominant failure mode for {args['service']} (TransactionError)",
        )
        logs = await _run_nrql(
            "SELECT count(*), latest(message), latest(trace.id) "
            f"FROM Log WHERE appName = '{app}' AND level IN "
            f"('ERROR','FATAL','error','fatal') {window} "
            "FACET `level` LIMIT 50",
            purpose=f"error-level log volume for {args['service']}",
        )
        return _text(
            "### TransactionError, grouped by class and transaction\n"
            + errors["content"][0]["text"]
            + "\n\n### Error-level log lines\n"
            + logs["content"][0]["text"]
        )

    # Attributes that identify a service on a Log event, in the order worth
    # trying. Which one carries data depends entirely on how logs are shipped
    # (APM forwarder, OTel, Fluent Bit, k8s plugin), so it has to be probed
    # rather than assumed.
    _LOG_SERVICE_KEYS = (
        "appName",
        "service.name",
        "entity.name",
        "service",
        "kubernetes.containerName",
        "container_name",
        "hostname",
        "host",
        "filePath",
    )

    @tool(
        "nr_logs",
        "Search actual log lines for a service. Use this instead of writing "
        "`FROM Log WHERE appName = ...` by hand: it first checks whether the "
        "account has any Log data in the window at all, then works out which "
        "attribute identifies your service (log shippers differ — appName, "
        "service.name, kubernetes.containerName), and only then returns lines. "
        "`contains` filters the message body. This is the right way to answer "
        "'are there logs for this?' — a bare appName filter returns zero rows "
        "on most accounts and looks identical to 'nothing went wrong'.",
        {"service": str, "window": str, "contains": str},
        annotations=_READ_ONLY,
    )
    async def nr_logs(args: dict[str, Any]) -> dict[str, Any]:
        window = args.get("window") or pb_window()
        service = (args.get("service") or "").strip()
        needle = (args.get("contains") or "").strip()
        out: list[str] = []

        # 1. Is there any log data at all in this window?
        rows, total = await _query(
            f"SELECT count(*) FROM Log {window}",
            purpose="is there any Log data in this window",
        )
        out.append("### Total Log volume in the window\n" + total["content"][0]["text"])
        volume = next(iter((rows or [{}])[0].values()), None) if rows else None
        if volume == 0:
            out.append(
                "\nThere are NO Log events in this account for this window. Logs "
                "are not a usable signal for this ticket — do not report 'no "
                "errors in the logs' as a finding. Set signals_confirmed.logs to "
                "'not_checked' and rely on TransactionError, Transaction and code."
            )
            return _text("\n\n".join(out))

        # 2. Which attribute identifies the service on a Log event?
        if service:
            probe = ", ".join(f"filter(count(*), WHERE `{k}` IS NOT NULL) AS `{k}`"
                              for k in _LOG_SERVICE_KEYS)
            present = await _run_nrql(
                f"SELECT {probe} FROM Log {window}",
                purpose="which service-identifying attributes exist on Log",
            )
            out.append(
                "### Which attributes are populated on Log events\n"
                + present["content"][0]["text"]
                + "\n(Zero means that attribute is absent — do not filter on it.)"
            )

            matches = await _run_nrql(
                "SELECT count(*) FROM Log WHERE "
                + " OR ".join(
                    f"`{k}` LIKE '%{_esc(service)}%'" for k in _LOG_SERVICE_KEYS
                )
                + f" {window} LIMIT MAX",
                purpose=f"does any attribute match service {service!r}",
            )
            out.append(
                f"### Log events matching {service!r} on any identifying attribute\n"
                + matches["content"][0]["text"]
            )

        # 3. The lines themselves.
        clauses = []
        if service:
            clauses.append(
                "(" + " OR ".join(
                    f"`{k}` LIKE '%{_esc(service)}%'" for k in _LOG_SERVICE_KEYS
                ) + ")"
            )
        if needle:
            clauses.append(f"message LIKE '%{_esc(needle)}%'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        lines = await _run_nrql(
            "SELECT timestamp, level, message, appName, `service.name`, `trace.id` "
            f"FROM Log{where} {window} ORDER BY timestamp DESC LIMIT 100",
            purpose=f"log lines for {service or 'all services'}"
            + (f" containing {needle!r}" if needle else ""),
        )
        out.append("### Log lines (newest first)\n" + lines["content"][0]["text"])
        return _text("\n\n".join(out))

    @tool(
        "nr_trace",
        "Fetch every span and log line sharing a trace.id, ordered in time. This "
        "is how you turn 'something failed' into 'this call, to this dependency, "
        "at this millisecond'.",
        {"trace_id": str, "window": str},
        annotations=_READ_ONLY,
    )
    async def nr_trace(args: dict[str, Any]) -> dict[str, Any]:
        tid = _esc(args["trace_id"])
        window = args.get("window") or pb_window()
        spans = await _run_nrql(
            "SELECT timestamp, name, service.name, duration.ms, "
            "otel.status_code, error.message FROM Span "
            f"WHERE trace.id = '{tid}' {window} ORDER BY timestamp LIMIT 200",
            purpose=f"span waterfall for trace {tid}",
        )
        logs = await _run_nrql(
            "SELECT timestamp, level, message, service.name FROM Log "
            f"WHERE trace.id = '{tid}' {window} ORDER BY timestamp LIMIT 200",
            purpose=f"correlated logs for trace {tid}",
        )
        return _text(
            "### Spans\n"
            + spans["content"][0]["text"]
            + "\n\n### Correlated logs\n"
            + logs["content"][0]["text"]
        )

    # ---------------------------------------------------------------- Databricks
    # (Disabled for now - not using Databricks in current flow)

    # @tool(
    #     "db_catalog",
    #     "List the tables you are allowed to read, with their purpose and key "
    #     "columns. Call this before writing any SQL — do not guess table names.",
    #     {},
    #     annotations=_READ_ONLY,
    # )
    # async def db_catalog(args: dict[str, Any]) -> dict[str, Any]:
    #     lines = ["Curated read-only tables:"]
    #     for name, meta in (pb.sql_templates or {}).items():
    #         lines.append(f"\n- template `{name}`: {meta.get('description', '')}")
    #         lines.append(f"  params: {list((meta.get('parameters') or {}).keys())}")
    #     lines.append("\nAd-hoc SELECTs are permitted on these tables only:")
    #     for t in (pb.services.get("_tables", {}) or {}).get("allow", []):
    #         lines.append(f"  {t}")
    #     return _text("\n".join(lines))
    #
    # @tool(
    #     "db_template",
    #     "Run a named, parameterised query from the playbook. Safest way to pull "
    #     "records for an entity you extracted from the ticket. Pass params as a "
    #     "JSON object of name -> value.",
    #     {"name": str, "params": str},
    #     annotations=_READ_ONLY,
    # )
    # async def db_template(args: dict[str, Any]) -> dict[str, Any]:
    #     tpl = (pb.sql_templates or {}).get(args["name"])
    #     if not tpl:
    #         return _text(
    #             f"No template named {args['name']!r}. Available: "
    #             f"{list((pb.sql_templates or {}).keys())}",
    #             is_error=True,
    #         )
    #     try:
    #         params = json.loads(args.get("params") or "{}")
    #     except json.JSONDecodeError as e:
    #         return _text(f"params is not valid JSON: {e}", is_error=True)
    #     missing = set(tpl.get("parameters", {})) - set(params)
    #     if missing:
    #         return _text(f"Missing parameters: {sorted(missing)}", is_error=True)
    #     try:
    #         res = db.query(tpl["sql"], parameters=params)
    #     except (SqlRejected, RuntimeError, TimeoutError) as e:
    #         trace.add("db_template", args, error=str(e))
    #         return _text(f"Query failed: {e}", is_error=True)
    #     trace.add(
    #         "db_template",
    #         {"name": args["name"], "params": params},
    #         result_summary=f"{res.row_count} rows",
    #         evidence_link=f"{settings.databricks.host}/sql/history?statementId="
    #         f"{res.statement_id}",
    #     )
    #     return _text(redactor.scrub(res.as_markdown()))
    #
    # @tool(
    #     "db_query",
    #     "Escape hatch: run an ad-hoc read-only SELECT. Use named parameter "
    #     "markers (:order_id) and pass values in `params` — never interpolate "
    #     "values from the ticket into the SQL string. Prefer db_template.",
    #     {"sql": str, "params": str, "purpose": str},
    #     annotations=_READ_ONLY,
    # )
    # async def db_query(args: dict[str, Any]) -> dict[str, Any]:
    #     try:
    #         params = json.loads(args.get("params") or "{}")
    #         res = db.query(args["sql"], parameters=params or None)
    #     except (SqlRejected, RuntimeError, TimeoutError, json.JSONDecodeError) as e:
    #         trace.add("db_query", args, error=str(e))
    #         return _text(f"Query rejected or failed: {e}", is_error=True)
    #     trace.add(
    #         "db_query",
    #         {"sql": res.statement, "params": params, "purpose": args.get("purpose")},
    #         result_summary=f"{res.row_count} rows",
    #         evidence_link=f"{settings.databricks.host}/sql/history?statementId="
    #         f"{res.statement_id}",
    #     )
    #     return _text(redactor.scrub(res.as_markdown()))

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
        # JQL text search treats most punctuation as reserved, so a raw error
        # message pasted straight in either errors or silently matches nothing.
        raw = args["text"][:300]
        terms = re.sub(r"[^\w\s.-]", " ", raw)
        words = [w for w in terms.split() if len(w) > 2][:12]
        if not words:
            return _text("Nothing searchable in that text.", is_error=True)
        phrase = " ".join(words)

        attempts = [
            f'project = {settings.jira.project_key} AND text ~ "{phrase}" '
            "ORDER BY created DESC",
            # Narrower fallback: the distinctive words only, summary-scoped.
            f'project = {settings.jira.project_key} AND summary ~ "'
            + " ".join(words[:5])
            + '" ORDER BY created DESC',
        ]
        keys: list[str] = []
        errors: list[str] = []
        jql = attempts[0]
        for candidate in attempts:
            try:
                keys = jira.search(candidate, limit=10)
            except Exception as e:  # noqa: BLE001 - surfaced to the model
                errors.append(f"{candidate[:60]}...: {e}")
                continue
            jql = candidate
            if keys:
                break
        if not keys and errors:
            trace.add("jira_related_tickets", {"jql": jql}, error="; ".join(errors)[:300])
            return _text("Jira search failed: " + "; ".join(errors), is_error=True)
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
        nr_apps,
        nr_event_types,
        nr_attributes,
        nr_find_errors,
        nr_logs,
        nr_trace,
        # db_catalog,
        # db_template,
        # db_query,
        jira_related_tickets,
    ]
    names = [
        "mcp__triage__nr_query",
        "mcp__triage__nr_apps",
        "mcp__triage__nr_event_types",
        "mcp__triage__nr_attributes",
        "mcp__triage__nr_find_errors",
        "mcp__triage__nr_logs",
        "mcp__triage__nr_trace",
        # "mcp__triage__db_catalog",
        # "mcp__triage__db_template",
        # "mcp__triage__db_query",
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
