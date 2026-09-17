"""Databricks SQL warehouse client via the Statement Execution API (2.0).

Chosen over databricks-sql-connector deliberately: it's a plain HTTP call, no
Thrift/ODBC driver to ship, and the async poll model survives long queries
without holding a socket open.

Two safety layers, because the SQL text is model-authored:
  1. Statement allowlist + forbidden-keyword scan in this process.
  2. Named parameter markers (:name) so extracted IDs are never string-concatenated.
The third layer lives outside the code: the token belongs to a service principal
with SELECT-only grants on a curated set of tables.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import DatabricksConfig

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY\s+INTO|VACUUM|OPTIMIZE|REFRESH|SET|USE|CALL)\b",
    re.IGNORECASE,
)
_ALLOWED_START = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")


class SqlRejected(ValueError):
    pass


@dataclass
class SqlResult:
    statement: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    statement_id: str

    def as_markdown(self, limit: int = 25) -> str:
        if not self.rows:
            return f"(0 rows)\n-- {self.statement}"
        head = " | ".join(self.columns)
        sep = " | ".join("---" for _ in self.columns)
        body = "\n".join(
            " | ".join("" if v is None else str(v) for v in row)
            for row in self.rows[:limit]
        )
        note = (
            f"\n\n({self.row_count} rows returned"
            f"{', truncated' if self.truncated else ''};"
            f" showing {min(limit, len(self.rows))})"
        )
        return f"{head}\n{sep}\n{body}{note}"


class DatabricksClient:
    def __init__(self, cfg: DatabricksConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self._http = client or httpx.Client(
            base_url=cfg.host,
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def validate(self, statement: str) -> str:
        s = statement.strip().rstrip(";")
        if ";" in s:
            raise SqlRejected("Multiple statements are not allowed.")
        if not s.upper().startswith(_ALLOWED_START):
            raise SqlRejected(
                f"Statement must start with one of {_ALLOWED_START}. Got: {s[:40]!r}"
            )
        if _FORBIDDEN.search(s):
            raise SqlRejected("Statement contains a write/DDL keyword; reads only.")
        return s

    def query(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        row_limit: int | None = None,
    ) -> SqlResult:
        s = self.validate(statement)
        body: dict[str, Any] = {
            "warehouse_id": self.cfg.warehouse_id,
            "statement": s,
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
            "row_limit": row_limit or self.cfg.row_limit,
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        }
        if self.cfg.catalog:
            body["catalog"] = self.cfg.catalog
        if self.cfg.schema:
            body["schema"] = self.cfg.schema
        if parameters:
            body["parameters"] = [
                {"name": k, "value": None if v is None else str(v), "type": "STRING"}
                for k, v in parameters.items()
            ]

        r = self._http.post("/api/2.0/sql/statements", json=body)
        r.raise_for_status()
        payload = r.json()
        payload = self._await_completion(payload)
        return self._to_result(s, payload)

    def _await_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        sid = payload["statement_id"]
        deadline = time.time() + self.cfg.timeout_s
        while payload["status"]["state"] in ("PENDING", "RUNNING"):
            if time.time() > deadline:
                self._http.post(f"/api/2.0/sql/statements/{sid}/cancel")
                raise TimeoutError(f"Statement {sid} exceeded {self.cfg.timeout_s}s")
            time.sleep(2)
            r = self._http.get(f"/api/2.0/sql/statements/{sid}")
            r.raise_for_status()
            payload = r.json()
        state = payload["status"]["state"]
        if state != "SUCCEEDED":
            err = payload["status"].get("error", {}).get("message", state)
            raise RuntimeError(f"Databricks statement {state}: {err}")
        return payload

    @staticmethod
    def _to_result(statement: str, payload: dict[str, Any]) -> SqlResult:
        manifest = payload.get("manifest") or {}
        cols = [
            c["name"]
            for c in ((manifest.get("schema") or {}).get("columns") or [])
        ]
        result = payload.get("result") or {}
        rows = result.get("data_array") or []
        return SqlResult(
            statement=statement,
            columns=cols,
            rows=rows,
            row_count=manifest.get("total_row_count", len(rows)),
            truncated=bool(manifest.get("truncated")),
            statement_id=payload["statement_id"],
        )
