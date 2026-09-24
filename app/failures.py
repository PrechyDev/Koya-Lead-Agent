"""One catalogue of everything that can go wrong outside our code (specs.md §10, E-50+).

Every failure has:
  * a plain-language message for the person using the app (a non-developer client):
    what happened, and what happens next — never stack traces or env var names;
  * an admin message: what broke and exactly how to fix it (goes to System issues + the alert);
  * `fatal`: if true the run stops immediately instead of letting the agent retry and spend.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureKind:
    code: str
    service: str
    severity: str  # info | warning | critical
    fatal: bool
    client: str
    admin: str


CATALOGUE: dict[str, FailureKind] = {k.code: k for k in [
    FailureKind("anthropic_no_credit", "anthropic", "critical", True,
                "The AI service has run out of credit, so this research couldn't run. Your admin has been told. "
                "Nothing was spent on company searches.",
                "Anthropic account is out of credit. Top up at console.anthropic.com → Billing (and check the "
                "workspace spend limit), then start the run again."),
    FailureKind("anthropic_auth", "anthropic", "critical", True,
                "The AI service isn't connected correctly, so this research couldn't run. Your admin has been told.",
                "Anthropic rejected ANTHROPIC_API_KEY (invalid or revoked). Create a new key and update it in "
                "Render → Environment."),
    FailureKind("anthropic_rate_limited", "anthropic", "warning", True,
                "The AI service is busy right now. Please try again in a few minutes.",
                "Anthropic returned 429 (rate limit). Wait a few minutes; if it keeps happening, check the "
                "workspace's rate limits in the Anthropic Console."),
    FailureKind("anthropic_unavailable", "anthropic", "warning", True,
                "The AI service is temporarily unavailable. Please try again shortly. Anything found so far is saved.",
                "Anthropic API error/overload (5xx/529) or the agent process failed. Retry later; see the run's "
                "technical detail."),
    FailureKind("agent_cannot_start", "app", "critical", True,
                "The research engine couldn't start on this server, so nothing was spent. Your admin has been told.",
                "The Claude Code CLI process couldn't be started. Most common cause on Windows: the web server was "
                "started with `uvicorn --reload`, whose event loop can't start subprocesses. Restart it WITHOUT "
                "--reload. (Linux, Docker and Render are not affected.) Otherwise check that the CLI bundled in "
                "claude-agent-sdk is present."),
    FailureKind("apify_no_credit", "apify", "critical", True,
                "Company search is unavailable: the search account has run out of credit. Your admin has been told.",
                "Apify account has no usage left this month (or the run's charge cap was hit). Check Apify Console "
                "→ Billing/Usage, or switch APIFY_TOKEN to the team account."),
    FailureKind("apify_auth", "apify", "critical", True,
                "Company search isn't set up correctly, so this research couldn't run. Your admin has been told.",
                "Apify rejected APIFY_TOKEN (invalid token). Copy the team account token from Apify Console → "
                "Settings → API & Integrations."),
    FailureKind("apify_actor_not_found", "apify", "critical", True,
                "Company search isn't set up correctly, so this research couldn't run. Your admin has been told.",
                "APIFY_ACTOR_ID doesn't exist or isn't accessible. Expected: harvestapi/linkedin-company-search."),
    FailureKind("apify_run_failed", "apify", "warning", True,
                "A company search didn't complete. To avoid being charged twice it was not retried automatically. "
                "Your admin has been told. Anything found so far is saved.",
                "An Apify actor run ended FAILED/TIMED-OUT/ABORTED. Open it in the Apify console (run id in the "
                "detail) before re-running — PRD rule: stop and ask."),
    FailureKind("apify_unavailable", "apify", "warning", True,
                "Company search is temporarily unavailable. Please try again shortly.",
                "Apify API returned a server error or didn't answer in time. Retry later; check status.apify.com."),
    FailureKind("firecrawl_no_credit", "firecrawl", "critical", True,
                "Website research is unavailable: the research account has run out of credit. Companies not yet "
                "researched are marked for review. Your admin has been told.",
                "Firecrawl returned 402 (no credits left). Top up or upgrade at firecrawl.dev → Billing."),
    FailureKind("firecrawl_auth", "firecrawl", "critical", True,
                "Website research isn't set up correctly. Your admin has been told.",
                "Firecrawl rejected FIRECRAWL_API_KEY. Create a new key at firecrawl.dev and update it in Render."),
    FailureKind("budget_exhausted", "budget", "critical", True,
                "This workspace has used its AI budget, so new research is paused. Your admin has been told.",
                "The app's Claude budget (CLAUDE_BUDGET_TOTAL_USD) would be exceeded. Raise it on purpose in "
                "Render → Environment, or wait."),
    FailureKind("budget_warning", "budget", "warning", False,
                "", "The app has used 80% or more of its Claude budget."),
    FailureKind("not_configured", "app", "critical", True,
                "The app isn't fully set up yet, so research can't start. Your admin has been told.",
                "Required settings are missing (see detail). Set them in Render → Environment and redeploy."),
    FailureKind("run_timeout", "app", "warning", False,
                "The research took too long and was stopped safely. Everything found so far is saved below.",
                "A run phase hit its watchdog timeout (see detail). Check the run's tool calls for where it stalled."),
    FailureKind("database_unavailable", "database", "critical", True,
                "The app can't reach its database right now. Please try again in a few minutes.",
                "Postgres is unreachable (Supabase paused, down, or the DSN/password changed). Check the Supabase "
                "dashboard; a paused free project needs 'Restore'."),
    FailureKind("auth_token_rejected", "app", "warning", False,
                "We couldn't confirm your sign-in. Please try again in a moment.",
                "Supabase issued a login token this server refused (e.g. 'not yet valid' or 'expired' right after "
                "sign-in). Usually this server's clock is off: on Docker Desktop, restart Docker (or run "
                "`wsl --shutdown`) to resync it; on Render this shouldn't happen. The detail names the exact error."),
    FailureKind("unexpected", "app", "critical", False,
                "Something went wrong on our side. Your admin has been told.",
                "Unhandled error (see detail and the server logs for the reference id)."),
]}


class ServiceFailure(Exception):
    """A classified failure. `detail` is technical (admins only); `.client` is safe to show anyone."""

    def __init__(self, code: str, detail: str = ""):
        self.kind = CATALOGUE[code]
        self.detail = detail
        super().__init__(f"{code}: {detail}")

    @property
    def code(self) -> str:
        return self.kind.code

    @property
    def client(self) -> str:
        return self.kind.client

    @property
    def fatal(self) -> bool:
        return self.kind.fatal


def message_for(failure: "ServiceFailure", is_admin: bool) -> str:
    """What a person sees: admins get the cause + the fix (they ARE the admin); members get the plain version."""
    if is_admin:
        return f"{failure.kind.admin} (Detail: {failure.detail})" if failure.detail else failure.kind.admin
    return failure.client


def admin_message_from_detail(error_detail: str | None) -> str | None:
    """Runs store error_detail as "[code] detail"; rebuild the admin message for the run page."""
    m = re.match(r"\[([a-z_]+)\]\s*(.*)", error_detail or "", re.S)
    if not m or m.group(1) not in CATALOGUE:
        return None
    kind = CATALOGUE[m.group(1)]
    return kind.admin + (f" (Detail: {m.group(2)[:300]})" if m.group(2) else "")


def classify_claude_error(status: int | None, text: str | None) -> str:
    """Map an Anthropic/Agent SDK error (status code and/or message text) to a catalogue code."""
    t = (text or "").lower()
    if "credit balance" in t or "insufficient credit" in t or "billing" in t or status == 402:
        return "anthropic_no_credit"
    if status in (401, 403) or "invalid api key" in t or "api key is invalid" in t or "authentication" in t:
        return "anthropic_auth"
    if status == 429 or "rate limit" in t or "rate_limit" in t:
        return "anthropic_rate_limited"
    return "anthropic_unavailable"


def classify_apify(status: int | None, text: str | None = "") -> str:
    t = (text or "").lower()
    if status in (401,) or "token is not valid" in t or "user-or-token-not-found" in t:
        return "apify_auth"
    if status == 404 or "record-not-found" in t or "actor with this name was not found" in t:
        return "apify_actor_not_found"
    if status == 402 or "usage" in t and "limit" in t or "not enough" in t or "insufficient" in t \
            or "platform-feature-disabled" in t or "max-total-charge" in t:
        return "apify_no_credit"
    if status == 403:
        return "apify_auth"
    return "apify_unavailable"


def looks_like_credit_problem(text: str) -> bool:
    return bool(re.search(r"credit balance|insufficient credit|out of credit", text or "", re.I))
