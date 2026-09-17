"""CLI: `triage EPS-1234`

Start here. Run it against 20-30 already-resolved tickets and compare the
agent's verdict to the real root cause before you let it post anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .agent import triage
from .config import Settings


def main() -> int:
    p = argparse.ArgumentParser(prog="triage", description="Agentic ticket triage")
    p.add_argument("ticket", help="Jira issue key, e.g. EPS-1234")
    p.add_argument("--playbook", help="Path to a playbook YAML")
    p.add_argument(
        "--post",
        action="store_true",
        help="Post the report as a Jira comment (default: print only)",
    )
    p.add_argument("--json", action="store_true", help="Emit the raw report JSON")
    p.add_argument("--model", help="Override the model, e.g. opus")
    args = p.parse_args()

    settings = Settings.from_env(args.playbook)
    if args.model:
        object.__setattr__(settings.agent, "model", args.model)

    outcome = asyncio.run(triage(args.ticket, settings, post=args.post))

    if args.json:
        print(json.dumps(outcome.report, indent=2))
    else:
        print(outcome.markdown)

    print(
        f"\n---\nrun={outcome.run_id} cost=${outcome.cost_usd:.3f} "
        f"trace={outcome.trace_path}"
        + (f" comment={outcome.posted_comment_id}" if outcome.posted_comment_id else "")
        + ("  [DRY RUN — nothing posted]" if not outcome.posted_comment_id else ""),
        file=sys.stderr,
    )
    return 0 if outcome.report or outcome.skipped_reason else 1


if __name__ == "__main__":
    raise SystemExit(main())
