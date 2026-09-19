"""Central configuration. Everything comes from env vars or a playbook YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@dataclass(frozen=True)
class JiraConfig:
    base_url: str          # https://yourcompany.atlassian.net
    email: str
    api_token: str
    project_key: str = "EPS"


@dataclass(frozen=True)
class NewRelicConfig:
    api_key: str           # User key (NRAK-...), not a licence key
    account_id: int
    endpoint: str = "https://api.newrelic.com/graphql"   # EU: api.eu.newrelic.com
    default_window: str = "SINCE 24 hours ago"
    max_rows: int = 200


@dataclass(frozen=True)
class DatabricksConfig:
    host: str              # https://dbc-xxxx.cloud.databricks.com
    token: str             # PAT or OAuth access token for a READ-ONLY service principal
    warehouse_id: str
    catalog: str | None = None
    schema: str | None = None
    row_limit: int = 200
    timeout_s: int = 120


@dataclass(frozen=True)
class AgentConfig:
    model: str = "sonnet"
    fallback_model: str = "haiku"
    max_turns: int = 40
    max_budget_usd: float = 2.00
    effort: str = "high"


@dataclass(frozen=True)
class GithubConfig:
    token: str             # fine-grained PAT: Contents:Read + Pull requests:Read
    api_url: str = "https://api.github.com"
    graphql_url: str = "https://api.github.com/graphql"


@dataclass
class Playbook:
    """Domain knowledge that makes the agent useful on *your* tickets."""

    name: str
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    nrql_templates: dict[str, str] = field(default_factory=dict)
    sql_templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_patterns: dict[str, str] = field(default_factory=dict)
    triage_notes: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Playbook":
        data = yaml.safe_load(Path(path).read_text())
        return cls(**data)

    def repo_paths(self) -> list[str]:
        return [s["repo_path"] for s in self.services.values() if s.get("repo_path")]


@dataclass(frozen=True)
class Settings:
    jira: JiraConfig
    newrelic: NewRelicConfig
    databricks: DatabricksConfig
    agent: AgentConfig
    playbook: Playbook
    github: GithubConfig | None = None   # optional: enables gh_* tools, no local clone needed
    dry_run: bool = True          # never writes to Jira unless explicitly disabled
    run_dir: Path = REPO_ROOT / ".runs"

    @classmethod
    def from_env(cls, playbook_path: str | None = None) -> "Settings":
        pb = playbook_path or os.environ.get(
            "TRIAGE_PLAYBOOK", str(REPO_ROOT / "config/playbooks/eps.yaml")
        )
        return cls(
            jira=JiraConfig(
                base_url=_req("JIRA_BASE_URL").rstrip("/"),
                email=_req("JIRA_EMAIL"),
                api_token=_req("JIRA_API_TOKEN"),
                project_key=os.environ.get("JIRA_PROJECT_KEY", "EPS"),
            ),
            newrelic=NewRelicConfig(
                api_key=_req("NEW_RELIC_API_KEY"),
                account_id=int(_req("NEW_RELIC_ACCOUNT_ID")),
                endpoint=os.environ.get(
                    "NEW_RELIC_ENDPOINT", "https://api.newrelic.com/graphql"
                ),
            ),
            databricks=DatabricksConfig(
                host=_req("DATABRICKS_HOST").rstrip("/"),
                token=_req("DATABRICKS_TOKEN"),
                warehouse_id=_req("DATABRICKS_WAREHOUSE_ID"),
                catalog=os.environ.get("DATABRICKS_CATALOG"),
                schema=os.environ.get("DATABRICKS_SCHEMA"),
            ),
            agent=AgentConfig(
                model=os.environ.get("TRIAGE_MODEL", "sonnet"),
                max_turns=int(os.environ.get("TRIAGE_MAX_TURNS", "40")),
                max_budget_usd=float(os.environ.get("TRIAGE_MAX_BUDGET_USD", "2.00")),
            ),
            playbook=Playbook.load(pb),
            github=_github_from_env(),
            dry_run=os.environ.get("TRIAGE_DRY_RUN", "1") != "0",
        )


def _github_from_env() -> GithubConfig | None:
    """GitHub is optional: only wired up if a token is present."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    return GithubConfig(
        token=token,
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        graphql_url=os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"),
    )
