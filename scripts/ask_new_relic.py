"""Standalone: ask a plain-English question, watch the AI turn it into an
NRQL query, and see the real New Relic result.

This is deliberately separate from the full triage flow (agent.py) — no Jira,
no gating, no report schema. Just: question -> model -> tool call -> NRQL ->
New Relic -> answer. Good for judging *why* the model picked the query it did.

Run it:
    source venv/bin/activate
    python scripts/ask_new_relic.py "why are order-service errors spiking?"

Or with no args, it drops you into a loop asking for questions one at a time.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Add workspace root to path so we can import src/
workspace_root = Path(__file__).parent.parent
sys.path.insert(0, str(workspace_root))

import ipdb
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolAnnotations,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from src.triage.clients.newrelic import NewRelicClient, NrqlRejected  # noqa: E402
from src.triage.config import Playbook, Settings  # noqa: E402

PLAYBOOK_PATH = "config/playbooks/eps.yaml"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_system_prompt(pb: Playbook) -> str:
    """The exact context the model gets about YOUR services and conventions.

    This is the piece worth reading closely if you want to know 'why did it
    write THAT NRQL' — it only knows what's written here plus the playbook
    notes below.
    """
    svc_lines = []
    for name, meta in pb.services.items():
        if name.startswith("_"):
            continue
        variants = meta.get("nr_app_variants") or [meta.get("nr_app_name", name)]
        svc_lines.append(f"- {name}: New Relic appName values = {variants}")

    return f"""You are a New Relic query assistant. You are given a plain
English question about production behaviour. Your job:

1. Decide which NRQL query would answer it (use FROM Log, TransactionError,
   Span, or Transaction as appropriate).
2. Call the `nr_query` tool with that NRQL and a one-line `purpose`.
3. Read the result and explain what it shows, in plain English.

# Services and their New Relic appName values (use these, not the repo name)
{chr(10).join(svc_lines)}

# Naming conventions
{pb.naming_conventions or "(none)"}

# Rules
- Always include a SINCE clause (e.g. `SINCE 1 hour ago`).
- Always include a LIMIT unless using TIMESERIES.
- Prefer FACET to group/count rather than dumping raw rows when the question
  is about "how many" or "which is worst".
- Call nr_query at most 2 times, then answer with what you found.
"""


def build_tool(nr: NewRelicClient):
    """One tool only: run an NRQL query. Mirrors tools.py's nr_query, trimmed
    down (no redaction/trace-file plumbing) so this script stays readable."""

    @tool(
        "nr_query",
        "Run a read-only NRQL query against New Relic and get back the rows.",
        {"nrql": str, "purpose": str},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def nr_query(args: dict) -> dict:
        nrql = args["nrql"]
        purpose = args.get("purpose", "")
        log(f"🔧 model wants to run NRQL (purpose: {purpose!r}):")
        log(f"   {nrql}")
        t0 = time.time()
        try:
            res = nr.nrql(nrql)
        except (NrqlRejected, RuntimeError) as e:
            log(f"❌ query failed after {(time.time()-t0)*1000:.0f}ms: {e}")
            return {"content": [{"type": "text", "text": f"Query failed: {e}"}], "isError": True}
        elapsed = (time.time() - t0) * 1000
        log(f"✅ New Relic returned {len(res.results)} row(s) in {elapsed:.0f}ms")
        log(f"   actual query sent (after auto SINCE/LIMIT): {res.query}")
        log(f"   permalink: {res.permalink}")
        body = json.dumps(res.results[:20], indent=2, default=str)
        return {"content": [{"type": "text", "text": f"Query: {res.query}\n\nResults:\n{body}"}]}

    server = create_sdk_mcp_server(name="nr", version="1.0.0", tools=[nr_query])
    return server, ["mcp__nr__nr_query"]


async def ask(question: str) -> None:
    log(f"🚀 question: {question!r}")

    settings = Settings.from_env(PLAYBOOK_PATH)
    pb = settings.playbook
    nr = NewRelicClient(settings.newrelic)

    system_prompt = build_system_prompt(pb)
    log("📖 system prompt built from eps.yaml services + naming_conventions "
        "(see build_system_prompt() to read it verbatim)")

    server, allowed = build_tool(nr)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={"nr": server},
        strict_mcp_config=True,
        allowed_tools=allowed,
        permission_mode="dontAsk",
        setting_sources=[],
        model=settings.agent.model,
        max_turns=6,
        env=settings.agent.provider_env(),
    )

    log(f"🤖 sending question to model={settings.agent.model} ...")
    async with ClaudeSDKClient(options=options) as client:
        await client.query(question)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        log(f"🤖 model says: {block.text.strip()}")
                    elif isinstance(block, ToolUseBlock):
                        log(f"🤖 model is calling tool: {block.name} args={block.input}")
            elif isinstance(message, ResultMessage):
                log(f"🏁 done. cost=${(message.total_cost_usd or 0):.4f}")


def main() -> None:
    if len(sys.argv) > 1:
        asyncio.run(ask(" ".join(sys.argv[1:])))
        return
    print("Type a question about your services (blank line to quit):")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        asyncio.run(ask(q))


if __name__ == "__main__":
    main()
