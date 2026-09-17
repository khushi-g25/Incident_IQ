"""Jira webhook -> background triage.

Phase 2 deployment. Jira webhooks time out fast, so accept, enqueue, return 202,
and do the work out of band. This uses a plain in-process task set to stay
dependency-light; swap `_enqueue` for Celery/SQS/Cloud Tasks when you need
retries and durability.

Jira Cloud webhooks don't sign requests, so the shared secret goes in the URL
query string and is compared in constant time. Put this behind your VPN or an
IP allowlist for Atlassian's published ranges as well.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request

from triage.agent import triage
from triage.config import Settings

log = logging.getLogger("triage.webhook")
WEBHOOK_SECRET = os.environ["TRIAGE_WEBHOOK_SECRET"]
TRIGGER_LABEL = os.environ.get("TRIAGE_TRIGGER_LABEL", "auto-triage")
MAX_CONCURRENT = int(os.environ.get("TRIAGE_MAX_CONCURRENT", "3"))

_sem = asyncio.Semaphore(MAX_CONCURRENT)
_inflight: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings.from_env()
    log.info("loaded playbook %s", app.state.settings.playbook.name)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/jira")
async def jira_webhook(
    request: Request,
    background: BackgroundTasks,
    secret: str = Query(...),
) -> dict[str, Any]:
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="bad secret")

    body = await request.json()
    issue = body.get("issue") or {}
    key = issue.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="no issue key")

    fields = issue.get("fields") or {}
    labels = fields.get("labels") or []

    # Opt-in by label. Do not auto-triage every ticket on day one: run it on a
    # labelled subset, measure, then widen.
    if TRIGGER_LABEL and TRIGGER_LABEL not in labels:
        return {"status": "skipped", "reason": f"missing label {TRIGGER_LABEL}"}

    if key in _inflight:
        return {"status": "skipped", "reason": "already running"}

    background.add_task(_run, request.app.state.settings, key)
    return {"status": "accepted", "ticket": key}


async def _run(settings: Settings, key: str) -> None:
    _inflight.add(key)
    try:
        async with _sem:
            outcome = await triage(key, settings)
        log.info(
            "triaged %s verdict=%s cost=%.3f trace=%s",
            key,
            (outcome.report or {}).get("verdict", "none"),
            outcome.cost_usd,
            outcome.trace_path,
        )
    except Exception:  # noqa: BLE001
        log.exception("triage failed for %s", key)
    finally:
        _inflight.discard(key)
