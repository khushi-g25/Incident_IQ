"""GitHub REST + GraphQL client.

Read-only, on purpose: no write endpoint is implemented here at all, so there
is nothing for a model-authored call to abuse even before the guardrails hook
sees it — same posture as the SELECT-only Databricks service principal.

Two APIs, for two different jobs:
  REST    -> file contents, commit history for a path, PRs for a commit,
             code search. Simple, stable, well-documented shapes.
  GraphQL -> line-level `blame`, which REST does not expose at all. This is
             the "git log -L on a repo you didn't clone" tool the README asks
             for instead of opening up Bash.

Everything reads from each repo's default branch (whatever it resolves to —
`main`, `master`, or otherwise), resolved once per repo and cached. There is no
way for a tool call to pin a stale or attacker-chosen ref.

Auth is a single Bearer token (fine-grained PAT scoped to Contents: Read-only
and Pull requests: Read-only on the repos in scope).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import GithubConfig

API_VERSION = "2022-11-28"

_BLAME_QUERY = """
query($owner: String!, $name: String!, $expr: String!, $path: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $expr) {
      ... on Commit {
        blame(path: $path) {
          ranges {
            startingLine
            endingLine
            commit {
              oid
              messageHeadline
              committedDate
              author { name }
              url
            }
          }
        }
      }
    }
  }
}
"""


class GithubRequestError(RuntimeError):
    pass


@dataclass
class GithubFile:
    repo: str
    path: str
    ref: str | None
    content: str
    sha: str
    html_url: str


@dataclass
class GithubCommit:
    sha: str
    author: str
    date: str
    message: str
    url: str


class GithubClient:
    def __init__(self, cfg: GithubConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self._default_branch_cache: dict[str, str] = {}
        self._http = client or httpx.Client(
            base_url=cfg.api_url,
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=30.0,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.get(path, params=params)
        if r.status_code == 404:
            raise GithubRequestError(f"{path} not found — check the repo name and token scope.")
        r.raise_for_status()
        return r.json()

    def default_branch(self, repo: str) -> str:
        """Resolved once per repo per process and cached — every read pins to this."""
        if repo not in self._default_branch_cache:
            info = self._get(f"/repos/{repo}")
            self._default_branch_cache[repo] = info.get("default_branch") or "main"
        return self._default_branch_cache[repo]

    def get_file(self, repo: str, path: str) -> GithubFile:
        branch = self.default_branch(repo)
        d = self._get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
        if isinstance(d, list):
            raise GithubRequestError(f"{path} is a directory, not a file.")
        if d.get("encoding") != "base64":
            raise GithubRequestError(f"Unexpected encoding for {path}: {d.get('encoding')}")
        content = base64.b64decode(d["content"]).decode("utf-8", errors="replace")
        return GithubFile(
            repo=repo, path=path, ref=branch, content=content,
            sha=d["sha"], html_url=d["html_url"],
        )

    def commits_for_path(
        self, repo: str, path: str, limit: int = 20, since: str | None = None,
    ) -> list[GithubCommit]:
        params: dict[str, Any] = {
            "path": path,
            "sha": self.default_branch(repo),
            "per_page": max(1, min(limit, 100)),
        }
        if since:
            params["since"] = since
        data = self._get(f"/repos/{repo}/commits", params=params)
        return [
            GithubCommit(
                sha=c["sha"],
                author=(c.get("commit", {}).get("author") or {}).get("name", "?"),
                date=(c.get("commit", {}).get("author") or {}).get("date", ""),
                message=(c.get("commit", {}).get("message") or "").split("\n")[0],
                url=c["html_url"],
            )
            for c in data
        ]

    def pulls_for_commit(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._get(f"/repos/{repo}/commits/{sha}/pulls", params={"per_page": 10})

    def search_code(self, repo: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self._get(
            "/search/code",
            params={"q": f"{query} repo:{repo}", "per_page": max(1, min(limit, 30))},
        )
        return data.get("items", [])

    def blame(self, repo: str, path: str) -> list[dict[str, Any]]:
        owner, name = repo.split("/", 1)
        branch = self.default_branch(repo)
        r = self._http.post(
            self.cfg.graphql_url,
            json={
                "query": _BLAME_QUERY,
                "variables": {"owner": owner, "name": name, "expr": branch, "path": path},
            },
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise GithubRequestError(f"GitHub GraphQL error: {payload['errors']}")
        obj = ((payload.get("data") or {}).get("repository") or {}).get("object")
        if not obj:
            raise GithubRequestError(f"No commit resolved for branch {branch!r} in {repo}.")
        ranges = (obj.get("blame") or {}).get("ranges") or []
        return [
            {
                "startingLine": rg["startingLine"],
                "endingLine": rg["endingLine"],
                "commit": {
                    "oid": rg["commit"]["oid"],
                    "message": rg["commit"]["messageHeadline"],
                    "committedDate": rg["commit"]["committedDate"],
                    "author": (rg["commit"].get("author") or {}).get("name", "?"),
                    "url": rg["commit"]["url"],
                },
            }
            for rg in ranges
        ]
