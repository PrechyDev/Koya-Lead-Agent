"""One catalogue of everything that can go wrong outside our code (specs.md §10, E-50+).

Every failure has:
  * a plain-language message for the person using the app (a non-developer client):
    what happened, and what happens next — never stack traces or env var names;
  * an admin message: what broke and exactly how to fix it (goes to System issues + the alert);
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureKind:
    code: str
    service: str
    severity: str  # info | warning | critical
    client: str
    admin: str


CATALOGUE: dict[str, FailureKind] = {k.code: k for k in [
    FailureKind("anthropic_no_credit", "anthropic", "critical",
                "The AI service has run out of credit, so this research couldn't run. Our support team has been told. "
                "Nothing was spent on company searches.",
                "Anthropic account is out of credit. Top up at console.anthropic.com → Billing (and check the "
                "workspace spend limit), then start the run again."),
    FailureKind("anthropic_auth", "anthropic", "critical",
                "The AI service isn't connected correctly, so this research couldn't run. Our support team has been told.",
                "Anthropic rejected ANTHROPIC_API_KEY (invalid or revoked). Create a new key and update it in "
                "Render → Environment."),
    FailureKind("anthropic_rate_limited", "anthropic", "warning",
                "The AI service is busy right now. Please try again in a few minutes.",
                "Anthropic returned 429 (rate limit). Wait a few minutes; if it keeps happening, check the "
                "workspace's rate limits in the Anthropic Console."),
    FailureKind("anthropic_unavailable", "anthropic", "warning",
                "The AI service is temporarily unavailable. Please try again shortly. Anything found so far is saved.",
                "Anthropic API error/overload (5xx/529) or the agent process failed. Retry later; see the run's "
                "technical detail."),
    FailureKind("agent_cannot_start", "app", "critical",
                "The research engine couldn't start on this server, so nothing was spent. Our support team has been told.",
                "The Claude Code CLI process couldn't be started. Most common cause on Windows: the web server was "
                "started with `uvicorn --reload`, whose event loop can't start subprocesses. Restart it WITHOUT "
                "--reload. (Linux, Docker and Render are not affected.) Otherwise check that the CLI bundled in "
                "claude-agent-sdk is present."),
    FailureKind("apify_no_credit", "apify", "critical",
                "Company search is unavailable: the search account has run out of credit. Our support team has been told.",
                "Apify account has no usage left this month (or the run's charge cap was hit). Check Apify Console "
                "→ Billing/Usage, or switch APIFY_TOKEN to the team account."),
    FailureKind("apify_auth", "apify", "critical",
                "Company search isn't set up correctly, so this research couldn't run. Our support team has been told.",
                "Apify rejected APIFY_TOKEN (invalid token). Copy the team account token from Apify Console → "
                "Settings → API & Integrations."),
    FailureKind("apify_actor_not_found", "apify", "critical",
                "Company search isn't set up correctly, so this research couldn't run. Our support team has been told.",
                "APIFY_ACTOR_ID doesn't exist or isn't accessible. Expected: harvestapi/linkedin-company-search."),
    FailureKind("apify_run_failed", "apify", "warning",
                "A company search didn't complete. To avoid being charged twice it was not retried automatically. "
                "Our support team has been told. Anything found so far is saved.",
                "An Apify actor run ended FAILED/TIMED-OUT/ABORTED. Open it in the Apify console (run id in the "
                "detail) before re-running — PRD rule: stop and ask."),
    FailureKind("apify_bad_input", "apify", "warning",
                "Company search couldn't understand this search, so this research stopped. Our support team has been told.",
                "Apify rejected the actor input (HTTP 400), e.g. an unknown location name or size band. See the "
                "tool call's detail, fix the input mapping in app/services/apify.py, then run again."),
    FailureKind("apify_unavailable", "apify", "warning",
                "Company search is temporarily unavailable. Please try again shortly.",
                "Apify API returned a server error or didn't answer in time. Retry later; check status.apify.com."),
    FailureKind("firecrawl_no_credit", "firecrawl", "critical",
                "Website research is unavailable: the research account has run out of credit. Companies not yet "
                "researched are marked for review. Our support team has been told.",
                "Firecrawl returned 402 (no credits left). Top up or upgrade at firecrawl.dev → Billing."),
    FailureKind("firecrawl_auth", "firecrawl", "critical",
                "Website research isn't set up correctly. Our support team has been told.",
                "Firecrawl rejected FIRECRAWL_API_KEY. Create a new key at firecrawl.dev and update it in Render."),
    FailureKind("budget_exhausted", "budget", "critical",
                "This workspace has used its AI budget, so new research is paused. Our support team has been told.",
                "The app's Claude budget would be exceeded. Top up the Anthropic account if needed, then raise the "
                "budget on the Spend page (developers)."),
    FailureKind("budget_requested", "budget", "warning", "",
                "An admin asked for more Claude budget. Top up the Anthropic account (Console → Plans & Billing, and "
                "raise its spend limit), then raise the budget on the Spend page; that resolves this request."),
    FailureKind("budget_warning", "budget", "warning",
                "", "The app has used 80% or more of its Claude budget."),
    FailureKind("not_configured", "app", "critical",
                "The app isn't fully set up yet, so research can't start. Our support team has been told.",
                "Required settings are missing (see detail). Set them in Render → Environment and redeploy."),
    FailureKind("run_limit_reached", "app", "warning",
                "The research reached this run's AI spending limit and stopped safely. Everything found so far is "
                "saved below.",
                "The run hit its per-run Claude cap (max_budget_usd) or its step limit (see detail). It's a safe "
                "stop, not an outage. Raise the run limits in app/config.py only on purpose (rule 4)."),
    FailureKind("run_timeout", "app", "warning",
                "The research took too long and was stopped safely. Everything found so far is saved below.",
                "A run phase hit its watchdog timeout (see detail). Check the run's tool calls for where it stalled."),
    FailureKind("database_unavailable", "database", "critical",
                "The app can't reach its database right now. Please try again in a few minutes.",
                "Postgres is unreachable (Supabase paused, down, or the DSN/password changed). Check the Supabase "
                "dashboard; a paused free project needs 'Restore'."),
    FailureKind("auth_token_rejected", "app", "warning",
                "We couldn't confirm your sign-in. Please try again in a moment.",
                "Supabase issued a login token this server refused (e.g. 'not yet valid' or 'expired' right after "
                "sign-in). Usually this server's clock is off: on Docker Desktop, restart Docker (or run "
                "`wsl --shutdown`) to resync it; on Render this shouldn't happen. The detail names the exact error."),
    FailureKind("unexpected", "app", "critical",
                "Something went wrong on our side. Our support team has been told.",
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



def message_for(failure: "ServiceFailure", technical: bool) -> str:
    """What a person sees: developers get the cause + the fix + the detail; everyone else (members AND admins)
    gets the plain version (D-90)."""
    if technical:
        return f"{failure.kind.admin} (Detail: {failure.detail})" if failure.detail else failure.kind.admin
    return failure.client


def admin_message_from_detail(error_detail: str | None) -> str | None:
    """Runs store error_detail as "[code] detail"; rebuild the technical message for the run page (developers)."""
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
    if "insufficient-permissions" in t:
        return "apify_auth"  # before the credit check below, where "insufficient" would read as no credit
    if status == 402 or "usage" in t and "limit" in t or "not enough" in t or "insufficient" in t \
            or "platform-feature-disabled" in t or "max-total-charge" in t:
        return "apify_no_credit"
    if status == 400 or "invalid-input" in t:
        return "apify_bad_input"  # our input mapping is wrong: not an outage, and never retried
    if status == 403:
        return "apify_auth"
    return "apify_unavailable"

