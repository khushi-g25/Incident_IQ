"""Jira Cloud REST client.

Read via API v2 (description/comments come back as plain wiki markup, which is far
easier to feed a model than v3's Atlassian Document Format tree).
Write via API v3 (comments must be ADF).
"""

from __future__ import annotations

import base64
import re
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
            # Labelled, not neutral. Handed over as a plain "## Comments"
            # section, a confident comment reads to the model as the answer and
            # the whole run becomes a paraphrase of it.
            parts.append(
                "\n## Comments — UNVERIFIED CLAIMS BY PEOPLE, NOT EVIDENCE\n"
                "These are what humans believed at the time. They are often "
                "wrong, out of date, or about a different incident. Treat each "
                "one as a hypothesis to test against New Relic and the code. "
                "Never cite a comment as the basis for a root cause."
            )
            for c in self.comments[-15:]:
                parts.append(f"[{c['created']}] {c['author']} claims: {c['body']}")
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
        """JQL search.

        Atlassian retired POST /rest/api/2/search in 2025; it answers 404/410 on
        current Cloud sites, which silently disabled related-ticket lookup. Try
        the replacement first and keep the old path as a fallback for Server/DC.
        """
        payload = {"jql": jql, "maxResults": limit, "fields": ["key"]}
        last: Exception | None = None
        for path in ("/rest/api/3/search/jql", "/rest/api/2/search"):
            try:
                r = self._http.post(path, json=payload)
                r.raise_for_status()
                return [i["key"] for i in r.json().get("issues", [])]
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (404, 410):
                    raise
                last = e
        raise RuntimeError(f"No usable Jira search endpoint: {last}")

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


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# Inline spans, in precedence order. Code is matched first so that markers
# inside a code span are treated as literal text.
_INLINE_RE = re.compile(
    r"""(?P<code>`[^`]+`)
      | (?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\))
      | (?P<strong>\*\*(?P<stext>[^*]+)\*\*|__(?P<stext2>[^_]+)__)
      | (?P<em>\*(?P<etext>[^*\n]+)\*|(?<![\w_])_(?P<etext2>[^_\n]+)_(?![\w_]))
    """,
    re.VERBOSE,
)


def _inline(text: str) -> list[dict[str, Any]]:
    """Markdown inline spans -> ADF text nodes with marks.

    ADF has no markdown parser, so `**bold**`, backticks, headings and tables
    all post as literal punctuation unless converted here. That is what made
    the generated comments look unformatted in Jira.
    """
    nodes: list[dict[str, Any]] = []

    def push(value: str, marks: list[dict[str, Any]] | None = None) -> None:
        if not value:
            return
        node: dict[str, Any] = {"type": "text", "text": value}
        if marks:
            node["marks"] = marks
        nodes.append(node)

    pos = 0
    for m in _INLINE_RE.finditer(text):
        push(text[pos:m.start()])
        if m.group("code"):
            push(m.group("code")[1:-1], [{"type": "code"}])
        elif m.group("link"):
            push(
                m.group("ltext"),
                [{"type": "link", "attrs": {"href": m.group("lurl")}}],
            )
        elif m.group("strong"):
            push(m.group("stext") or m.group("stext2"), [{"type": "strong"}])
        else:
            push(m.group("etext") or m.group("etext2"), [{"type": "em"}])
        pos = m.end()
    push(text[pos:])
    return nodes or [{"type": "text", "text": text}]


def _para(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": _inline(text)}


def _cells(row: str, header: bool) -> list[dict[str, Any]]:
    kind = "tableHeader" if header else "tableCell"
    return [
        {"type": kind, "attrs": {}, "content": [_para(c.strip())]}
        for c in row.split("|")
    ]


def _to_adf(text: str) -> dict[str, Any]:
    """Markdown -> ADF.

    Supports headings, bold/italic/inline-code/links, bullet and ordered
    lists, fenced code, horizontal rules and pipe tables — i.e. everything
    `render_markdown` actually emits.
    """
    content: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # fenced code
        if stripped.startswith("```"):
            lang = stripped[3:].strip() or "text"
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1  # closing fence
            content.append(
                {
                    "type": "codeBlock",
                    "attrs": {"language": lang},
                    "content": [{"type": "text", "text": "\n".join(block)}]
                    if block
                    else [],
                }
            )
            continue

        if _RULE_RE.match(line):
            content.append({"type": "rule"})
            i += 1
            continue

        if m := _HEADING_RE.match(stripped):
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": min(len(m.group(1)), 6)},
                    "content": _inline(m.group(2)),
                }
            )
            i += 1
            continue

        # pipe table: header row, separator, then body rows
        if (
            _TABLE_ROW_RE.match(line)
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            header = _TABLE_ROW_RE.match(line).group(1)
            rows = [{"type": "tableRow", "content": _cells(header, True)}]
            i += 2
            while i < len(lines) and (rm := _TABLE_ROW_RE.match(lines[i])):
                rows.append({"type": "tableRow", "content": _cells(rm.group(1), False)})
                i += 1
            content.append({"type": "table", "attrs": {"isNumberColumnEnabled": False},
                            "content": rows})
            continue

        # lists
        for pattern, node_type in ((_BULLET_RE, "bulletList"), (_ORDERED_RE, "orderedList")):
            if pattern.match(line):
                items = []
                while i < len(lines) and (lm := pattern.match(lines[i])):
                    items.append(
                        {"type": "listItem", "content": [_para(lm.group(1))]}
                    )
                    i += 1
                content.append({"type": node_type, "content": items})
                break
        else:
            # paragraph: consume until a blank line or a line that starts a
            # different block, so wrapped prose stays one paragraph.
            para: list[str] = []
            while i < len(lines) and lines[i].strip():
                nxt = lines[i]
                if para and (
                    _HEADING_RE.match(nxt.strip())
                    or _BULLET_RE.match(nxt)
                    or _ORDERED_RE.match(nxt)
                    or _RULE_RE.match(nxt)
                    or _TABLE_ROW_RE.match(nxt)
                    or nxt.strip().startswith("```")
                ):
                    break
                para.append(nxt.strip())
                i += 1
            content.append(_para(" ".join(para)))

    return {
        "type": "doc",
        "version": 1,
        "content": content
        or [{"type": "paragraph", "content": [{"type": "text", "text": text[:3000]}]}],
    }
