"""All configuration in one place (specs.md §7).

Values come from environment variables (.env locally, Render env vars in
production). Nothing secret has a default. Limit presets and model prices live
here so every module reads the same numbers.
"""

import re
from dataclasses import asdict, dataclass
from decimal import ROUND_UP, Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Claude ---
    anthropic_api_key: str = ""
    model_orchestrator: str = "claude-sonnet-5"
    model_icp: str = "claude-haiku-4-5"          # A/B (D-77): same score as Sonnet at ~1/3 the cost
    model_researcher: str = "claude-opus-5-5"    # owner (D-77): most careful qualifier; Sonnet had 1 false "qualified"
    model_copywriter: str = "claude-opus-5-5"    # A/B (D-77): 0 invented claims; cheaper than Sonnet here
    model_grounding: str = "claude-haiku-4-5"
    model_checks: str = "claude-haiku-4-5"  # the small checks: scope check before a run + the credit probe
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
    render_external_url: str = ""
    alert_webhook_url: str = ""
    alert_webhook_secret: str = ""
    cookie_secure: bool | None = Field(default=None)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_default(cls, value, info):
        # An empty value (`APIFY_ACTOR_ID=` in .env, or a blank Render variable) means "use the default", for
        # every setting: a blank bool/int would otherwise stop the app from starting.
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
    def to_dict(self) -> dict:
        return asdict(self)


# Per-run Claude cap, sized from the run (owner, 2026-09-25, D-97) instead of two fixed numbers. Unit costs are
# measured from the 2026-09-25 live runs; adjust them here if prices or models change.
RESEARCH_COST_PER_COMPANY_USD = Decimal("0.13")  # Opus researcher ~$0.11 + orchestrator ~$0.02 per company
DRAFT_COST_PER_LEAD_USD = Decimal("0.05")  # Opus copywriter, per qualified lead
RUN_OVERHEAD_USD = Decimal("0.05")  # ICP step, run start and finish
RUN_CAP_MARGIN = Decimal("1.15")  # room for a harder-than-usual company


def companies_budgeted(target: int, max_candidates: int) -> int:
    """How many companies a run is budgeted to research: about 2 per lead wanted (half of discovery results
    usually aren't fits), plus 2, never more than the run's candidate limit."""
    return min(int(max_candidates), 2 * int(target) + 2)


def run_cap_usd(target: int, max_candidates: int) -> float:
    """The run's Claude cap: e.g. 1 lead -> $0.72, 3 -> $1.43, 10 -> $3.63. A ceiling, not a charge."""
    raw = (companies_budgeted(target, max_candidates) * RESEARCH_COST_PER_COMPANY_USD
           + int(target) * DRAFT_COST_PER_LEAD_USD + RUN_OVERHEAD_USD) * RUN_CAP_MARGIN
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_UP))


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
    max_budget_usd=run_cap_usd(10, 20),  # sized from the run (D-97); was a fixed $3.00 (D-83)
    max_outreach_rewrites=2,
    max_tool_calls=120,
    phase_timeout_s=1800,
)

DEV_LIMITS = RunLimits(
    target_qualified=1,  # D-91: test runs aim for one lead
    first_pool=3,
    topup_size=2,
    max_candidates=5,
    max_discovery_calls=2,
    apify_max_charge_usd=0.05,
    max_scrapes=4,
    max_pages_per_domain=1,
    max_turns=30,
    max_budget_usd=run_cap_usd(1, 5),  # sized from the run (D-97); was a fixed $0.30
    max_outreach_rewrites=1,
    max_tool_calls=40,
    phase_timeout_s=900,
)

# How this tool introduces itself in shared emails (docs/email-templates/, D-65). Sent with each invite as
# user metadata, so one Supabase template can serve every Koya tool.
APP_NAME = "Koya Lead Research Agent"
APP_TAGLINE = ("the tool Koya's outbound team uses to find and research companies that may need an AI automation "
               "assistant, and to draft outreach for human review")

ICP_PHASE_MAX_TURNS = 6
GROUNDING_RUN_CAP_USD = Decimal("0.10")  # fact-check spend allowed per run, on top of the run's Claude cap
TRIAGE_CALL_CAP_USD = Decimal("0.02")  # one candidate-triage call (Haiku, ~$0.005); skipped if it can't fit (D-98)
ICP_PHASE_MAX_BUDGET_USD = 0.05
ICP_PHASE_TIMEOUT_S = 240


def limits_for_run(target_qualified: int, dev: bool) -> RunLimits:
    base = DEV_LIMITS if dev else FULL_LIMITS
    target = max(1, min(int(target_qualified), base.target_qualified))
    return RunLimits(**{**base.to_dict(), "target_qualified": target,
                        "max_budget_usd": run_cap_usd(target, base.max_candidates)})


# ---------------------------------------------------------------------------
# Model prices, USD per 1M tokens: (input, output, cache read). Used for calls made outside the Agent SDK
# (fact-checks, scope check, cost recovered from transcripts); the SDK reports its own cost.
# Cache reads are each model's real price, not one multiplier: Opus 5.5 reads its cache at $0.20 (0.05x its
# input price), the others at the standard 0.1x. Cache writes are 1.25x input for all of them.
# ---------------------------------------------------------------------------
MODEL_PRICES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1"), Decimal("5"), Decimal("0.10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("0.20")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("0.20")),
}
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
BATCH_DISCOUNT = Decimal("0.5")
