"""All configuration in one place (specs.md §7).

Values come from environment variables (.env locally, Render env vars in
production). Nothing secret has a default. Limit presets and model prices live
here so every module reads the same numbers.
"""

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Claude ---
    anthropic_api_key: str = ""
    model_orchestrator: str = "claude-sonnet-5"
    model_icp: str = "claude-sonnet-5"
    model_researcher: str = "claude-sonnet-5"
    model_copywriter: str = "claude-sonnet-5"
    model_grounding: str = "claude-haiku-4-5"
    claude_budget_total_usd: Decimal = Decimal("6.00")

    # --- Apify / Firecrawl ---
    apify_token: str = ""
    apify_actor_id: str = "harvestapi/linkedin-company-search"
    firecrawl_api_key: str = ""

    # --- Database ---
    supabase_db_dsn: str = ""
    supabase_admin_dsn: str = ""
    supabase_db_schema: str = "lead_agent"

    # --- Supabase Auth (server-side only) ---
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwks_url: str = ""
    supabase_jwt_audience: str = "authenticated"

    # --- App ---
    session_secret: str = ""
    app_base_url: str = "http://localhost:8000"
    research_reuse_days: int = 30
    scrape_cache_days: int = 7
    dev_limits: bool = True
    fixture_mode: bool = False
    max_runs_per_day: int = 5
    max_runs_per_user_per_day: int = 2
    max_runs_per_admin_per_day: int = 5
    max_parallel_subagents: int | None = None  # optional override of the preset (e.g. 2 if Render memory is tight)
    render_external_url: str = ""
    alert_webhook_url: str = ""
    alert_webhook_secret: str = ""
    cookie_secure: bool | None = Field(default=None)

    @field_validator("apify_actor_id", "model_orchestrator", "model_icp", "model_researcher", "model_copywriter",
                     "model_grounding", "max_parallel_subagents", mode="before")
    @classmethod
    def _blank_means_default(cls, value, info):
        # An empty line like `APIFY_ACTOR_ID=` in .env must not override the default with "".
        if value is None or (isinstance(value, str) and not value.strip()):
            return cls.model_fields[info.field_name].default
        return value.strip() if isinstance(value, str) else value

    def missing_run_config(self) -> list[str]:
        """Settings a research run can't work without (checked before any spend)."""
        required = {"ANTHROPIC_API_KEY": self.anthropic_api_key, "APIFY_TOKEN": self.apify_token,
                    "APIFY_ACTOR_ID": self.apify_actor_id, "FIRECRAWL_API_KEY": self.firecrawl_api_key,
                    "SUPABASE_DB_DSN": self.supabase_db_dsn}
        return [name for name, value in required.items() if not value]

    @field_validator("supabase_db_schema")
    @classmethod
    def _schema_is_safe(cls, value: str) -> str:
        # The schema name is inserted into SQL text, so only allow plain identifiers.
        if not re.fullmatch(r"[a-z_]+", value):
            raise ValueError("SUPABASE_DB_SCHEMA must match ^[a-z_]+$")
        return value

    @property
    def jwks_url(self) -> str:
        return self.supabase_jwks_url or f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Run limits (specs.md §7.1). Stored on each run record when it's created;
# tools read them from the DB row, never from agent input.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunLimits:
    target_qualified: int
    first_pool: int
    topup_size: int
    max_candidates: int
    max_discovery_calls: int
    apify_max_charge_usd: float
    max_scrapes: int
    max_pages_per_domain: int
    max_turns: int
    max_budget_usd: float
    max_outreach_rewrites: int
    max_tool_calls: int
    phase_timeout_s: int
    max_parallel_subagents: int = 1  # researchers/copywriters working at the same time (D-49)

    def to_dict(self) -> dict:
        return asdict(self)


FULL_LIMITS = RunLimits(
    target_qualified=10,
    first_pool=12,
    topup_size=5,
    max_candidates=20,
    max_discovery_calls=3,
    apify_max_charge_usd=0.25,
    max_scrapes=20,
    max_pages_per_domain=2,
    max_turns=60,
    max_budget_usd=1.25,
    max_outreach_rewrites=2,
    max_tool_calls=120,
    phase_timeout_s=1800,
    max_parallel_subagents=3,
)

DEV_LIMITS = RunLimits(
    target_qualified=2,
    first_pool=3,
    topup_size=2,
    max_candidates=5,
    max_discovery_calls=2,
    apify_max_charge_usd=0.05,
    max_scrapes=4,
    max_pages_per_domain=1,
    max_turns=30,
    max_budget_usd=0.30,
    max_outreach_rewrites=1,
    max_tool_calls=40,
    phase_timeout_s=900,
    max_parallel_subagents=2,
)

ICP_PHASE_MAX_TURNS = 6
ICP_PHASE_MAX_BUDGET_USD = 0.05
ICP_PHASE_TIMEOUT_S = 240


def limits_for_run(target_qualified: int, dev: bool) -> RunLimits:
    base = DEV_LIMITS if dev else FULL_LIMITS
    target = max(1, min(int(target_qualified), base.target_qualified))
    override = get_settings().max_parallel_subagents
    parallel = max(1, min(int(override), 5)) if override else base.max_parallel_subagents
    return RunLimits(**{**base.to_dict(), "target_qualified": target, "max_parallel_subagents": parallel})


# ---------------------------------------------------------------------------
# Model prices, USD per 1M tokens (input, output). Used for calls made outside
# the Agent SDK (grounding, evals); the SDK reports its own cost.
# ---------------------------------------------------------------------------
MODEL_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
}
CACHE_READ_MULTIPLIER = Decimal("0.1")
BATCH_DISCOUNT = Decimal("0.5")
