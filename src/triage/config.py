"""Central configuration. Everything comes from env vars or a playbook YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

# Load environment variables from .env file
load_dotenv(REPO_ROOT / ".env")


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _bedrock_enabled() -> bool:
    """Bedrock is used when asked for explicitly, or when the only credential
    present is a Bedrock one."""
    flag = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if flag in {"0", "false", "no"}:
        return False
    return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK")) and not os.environ.get(
        "ANTHROPIC_API_KEY"
    )


@dataclass(frozen=True)
class JiraConfig:
    base_url: str          # https://yourcompany.atlassian.net
    email: str
    api_token: str
    project_key: str = "SQ"


@dataclass(frozen=True)
class NewRelicConfig:
    api_key: str           # User key (NRAK-...), not a licence key
    account_id: int
    endpoint: str = "https://api.newrelic.com/graphql"   # EU: api.eu.newrelic.com
    default_window: str = "SINCE 24 hours ago"
    max_rows: int = 200


# @dataclass(frozen=True)
# class DatabricksConfig:
#     host: str              # https://dbc-xxxx.cloud.databricks.com
#     token: str             # PAT or OAuth access token for a READ-ONLY service principal
#     warehouse_id: str
#     catalog: str | None = None
#     schema: str | None = None
#     row_limit: int = 200
#     timeout_s: int = 120


@dataclass(frozen=True)
class AgentConfig:
    model: str = "sonnet"
    fallback_model: str | None = None
    max_turns: int = 40
    max_budget_usd: float = 2.00
    effort: str = "high"
    # --- provider auth (Anthropic API by default, Amazon Bedrock if enabled) ---
    use_bedrock: bool = False
    aws_region: str | None = None
    aws_bearer_token_bedrock: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_profile: str | None = None

    def provider_env(self) -> dict[str, str]:
        """Env vars the Claude Code subprocess needs to reach the right provider.

        The SDK spawns a Node CLI as a child process; anything it needs for auth
        has to be handed over explicitly rather than assumed to be inherited.
        """
        if not self.use_bedrock:
            return {}
        env: dict[str, str] = {"CLAUDE_CODE_USE_BEDROCK": "1"}
        for key, val in (
            ("AWS_REGION", self.aws_region),
            ("AWS_DEFAULT_REGION", self.aws_region),
            ("AWS_BEARER_TOKEN_BEDROCK", self.aws_bearer_token_bedrock),
            ("AWS_ACCESS_KEY_ID", self.aws_access_key_id),
            ("AWS_SECRET_ACCESS_KEY", self.aws_secret_access_key),
            ("AWS_SESSION_TOKEN", self.aws_session_token),
            ("AWS_PROFILE", self.aws_profile),
        ):
            if val:
                env[key] = val
        return env


@dataclass
class Playbook:
    """Domain knowledge that makes the agent useful on *your* tickets."""

    name: str
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    nrql_templates: dict[str, str] = field(default_factory=dict)
    sql_templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_patterns: dict[str, str] = field(default_factory=dict)
    naming_conventions: dict[str, str] = field(default_factory=dict)
    playbooks: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    # databricks: DatabricksConfig
    agent: AgentConfig
    playbook: Playbook
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
            # databricks=DatabricksConfig(
            #     host=_req("DATABRICKS_HOST").rstrip("/"),
            #     token=_req("DATABRICKS_TOKEN"),
            #     warehouse_id=_req("DATABRICKS_WAREHOUSE_ID"),
            #     catalog=os.environ.get("DATABRICKS_CATALOG"),
            #     schema=os.environ.get("DATABRICKS_SCHEMA"),
            # ),
            agent=AgentConfig(
                model=os.environ.get("TRIAGE_MODEL", "sonnet"),
                fallback_model=os.environ.get("TRIAGE_FALLBACK_MODEL") or None,
                max_turns=int(os.environ.get("TRIAGE_MAX_TURNS", "40")),
                max_budget_usd=float(os.environ.get("TRIAGE_MAX_BUDGET_USD", "2.00")),
                use_bedrock=_bedrock_enabled(),
                aws_region=os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION"),
                aws_bearer_token_bedrock=os.environ.get("AWS_BEARER_TOKEN_BEDROCK"),
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
                aws_profile=os.environ.get("AWS_PROFILE"),
            ),
            playbook=Playbook.load(pb),
            dry_run=os.environ.get("TRIAGE_DRY_RUN", "1") != "0",
        )
