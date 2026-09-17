"""Jira Cloud REST client.

Read via API v2 (description/comments come back as plain wiki markup, which is far
easier to feed a model than v3's Atlassian Document Format tree).
Write via API v3 (comments must be ADF).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import JiraConfig

FIELDS = (
    "summary,description,status,priority,labels,components,issuetype,"
    "created,updated,reporter,assignee,comment,attachment,customfield_10000"
)


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str
    status: str
    priority: str
    labels: list[str]
    components: list[str]
    created: str
    comments: list[dict[str, str]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_prompt_text(self) -> str:
        parts = [
            f"# {self.key}: {self.summary}",
            f"status={self.status} priority={self.priority} created={self.created}",
            f"labels={', '.join(self.labels) or 'none'} "
            f"components={', '.join(self.components) or 'none'}",
            "\n## Description\n" + (self.description or "(empty)"),
        ]
        if self.comments:
            parts.append("\n## Comments")
            for c in self.comments[-15:]:
                parts.append(f"[{c['created']}] {c['author']}: {c['body']}")
        if self.attachments:
            names = ", ".join(a["filename"] for a in self.attachments)
            parts.append(f"\n## Attachments\n{names}")
        return "\n".join(parts)


class JiraClient:
    def __init__(self, cfg: JiraConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        token = base64.b64encode(
            f"{cfg.email}:{cfg.api_token}".encode()
        ).decode()
        self._http = client or httpx.Client(
            base_url=cfg.base_url,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def get_issue(self, key: str) -> JiraIssue:
        r = self._http.get(f"/rest/api/2/issue/{key}", params={"fields": FIELDS})
        r.raise_for_status()
        d = r.json()
        f = d["fields"]
        return JiraIssue(
            key=d["key"],
            summary=f.get("summary") or "",
            description=f.get("description") or "",
            status=(f.get("status") or {}).get("name", "?"),
            priority=(f.get("priority") or {}).get("name", "?"),
            labels=f.get("labels") or [],
            components=[c["name"] for c in (f.get("components") or [])],
            created=f.get("created", ""),
            comments=[
                {
                    "author": (c.get("author") or {}).get("displayName", "?"),
                    "created": c.get("created", ""),
                    "body": c.get("body") or "",
                }
                for c in ((f.get("comment") or {}).get("comments") or [])
            ],
            attachments=[
                {
                    "filename": a["filename"],
                    "mimeType": a.get("mimeType"),
                    "size": a.get("size"),
                    "content": a.get("content"),
                }
                for a in (f.get("attachment") or [])
            ],
            raw=d,
        )

    def search(self, jql: str, limit: int = 20) -> list[str]:
        r = self._http.post(
            "/rest/api/2/search",
            json={"jql": jql, "maxResults": limit, "fields": ["key"]},
        )
        r.raise_for_status()
        return [i["key"] for i in r.json().get("issues", [])]

    def fetch_attachment_text(self, content_url: str, max_bytes: int = 400_000) -> str:
        """Pull a text/log attachment. Binary types are skipped by the caller."""
        r = self._http.get(content_url, follow_redirects=True)
        r.raise_for_status()
        return r.content[:max_bytes].decode("utf-8", errors="replace")

    def add_comment(self, key: str, markdown: str) -> str:
        r = self._http.post(
            f"/rest/api/3/issue/{key}/comment",
            json={"body": _to_adf(markdown)},
        )
        r.raise_for_status()
        return r.json()["id"]


def _to_adf(text: str) -> dict[str, Any]:
    """Minimal Markdown -> ADF. Handles paragraphs, fenced code, and bullets."""
    content: list[dict[str, Any]] = []
    in_code = False
    buf: list[str] = []

    def flush_para() -> None:
        if not buf:
            return
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "\n".join(buf)}],
            }
        )
        buf.clear()

    def flush_code() -> None:
        if not buf:
            return
        content.append(
            {
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": "\n".join(buf)}],
            }
        )
        buf.clear()

    for line in text.splitlines():
        if line.strip().startswith("```"):
            flush_code() if in_code else flush_para()
            in_code = not in_code
            continue
        if in_code:
            buf.append(line)
        elif not line.strip():
            flush_para()
        else:
            buf.append(line)
    flush_code() if in_code else flush_para()

    return {"type": "doc", "version": 1, "content": content or [
        {"type": "paragraph", "content": [{"type": "text", "text": text[:3000]}]}
    ]}
