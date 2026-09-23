"""Approve/reject endpoint behaviour.

The Approve button posts the only thing this system ever writes anywhere, so
its failure modes matter: a lost pending run, or a Jira rejection swallowed
into an unhelpful 500.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service import ui  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_PENDING_DIR", tmp_path / "pending")
    ui._RUNS.clear()
    with TestClient(ui.app) as c:
        yield c


class _FakeJira:
    """Stands in for JiraClient; records or refuses the post."""

    posted: list[tuple[str, str]] = []
    raise_with: Exception | None = None

    def __init__(self, cfg):
        pass

    def add_comment(self, key: str, markdown: str) -> str:
        if _FakeJira.raise_with:
            raise _FakeJira.raise_with
        _FakeJira.posted.append((key, markdown))
        return "10042"


@pytest.fixture(autouse=True)
def fake_jira(monkeypatch):
    _FakeJira.posted = []
    _FakeJira.raise_with = None
    monkeypatch.setattr(ui, "JiraClient", _FakeJira)
    return _FakeJira


def _stage(run_id="abc123", ticket="SQ-1794"):
    ui._remember(run_id, {"ticket": ticket, "markdown": "# report", "report": {}})
    return run_id


def test_approve_posts_the_comment_and_returns_a_browse_url(client, fake_jira):
    run_id = _stage()
    r = client.post(f"/api/approve/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "posted"
    assert body["comment_id"] == "10042"
    assert body["browse_url"].endswith("/browse/SQ-1794")
    assert fake_jira.posted == [("SQ-1794", "# report")]


def test_pending_run_survives_a_server_reload(client, fake_jira):
    """--reload restarts the worker on every file save; holding pending runs
    only in memory made Approve answer 'unknown run_id'."""
    run_id = _stage()
    ui._RUNS.clear()                       # what a reload does
    assert client.post(f"/api/approve/{run_id}").status_code == 200
    assert fake_jira.posted


def test_jira_failure_surfaces_the_real_reason_not_a_bare_500(client, fake_jira):
    fake_jira.raise_with = ui.JiraCommentError(
        "Jira returned 403. The account lacks permission to comment."
    )
    r = client.post(f"/api/approve/{_stage()}")
    assert r.status_code == 502
    assert "403" in r.json()["detail"]
    assert "lacks permission" in r.json()["detail"]


def test_a_failed_post_keeps_the_report_so_it_can_be_retried(client, fake_jira):
    run_id = _stage()
    fake_jira.raise_with = RuntimeError("network down")
    assert client.post(f"/api/approve/{run_id}").status_code == 502

    fake_jira.raise_with = None
    assert client.post(f"/api/approve/{run_id}").status_code == 200
    assert fake_jira.posted


def test_approving_twice_does_not_post_twice(client, fake_jira):
    run_id = _stage()
    assert client.post(f"/api/approve/{run_id}").status_code == 200
    assert client.post(f"/api/approve/{run_id}").status_code == 404
    assert len(fake_jira.posted) == 1


def test_reject_discards_without_posting(client, fake_jira):
    run_id = _stage()
    assert client.post(f"/api/reject/{run_id}").json()["status"] == "discarded"
    assert client.post(f"/api/approve/{run_id}").status_code == 404
    assert fake_jira.posted == []
