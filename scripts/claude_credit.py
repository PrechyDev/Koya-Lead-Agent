"""Is the Anthropic key working, and how much has this project spent on Claude?

    .venv/Scripts/python scripts/claude_credit.py              # 1-token test call (~$0.00001) + spend report
    .venv/Scripts/python scripts/claude_credit.py --no-probe   # spend report only (free)

What it can and can't tell you:
* "Can the key spend right now?"  Yes: a 1-token call. Out of credit -> Anthropic refuses it (free).
* "How much has the app spent?"   Yes: every Claude call is recorded in lead_agent.spend_ledger (rule 5).
* "How much credit is LEFT on the Anthropic account?"  No: a normal API key can't see the balance. Only the
  Anthropic Console (Plans & Billing) or the Admin API (needs an admin key) shows it.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.services import health  # noqa: E402

log = logging.getLogger("lead_agent.scripts.claude_credit")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-probe", action="store_true", help="skip the 1-token test call")
    args = parser.parse_args()
    settings = get_settings()

    if not args.no_probe:
        failure = health.check_anthropic()  # the same probe the app runs before every research run
        if failure is None:
            log.info("Anthropic key: WORKING (a 1-token test call succeeded, cost ~$0.00001)")
        elif failure.code == "anthropic_no_credit":
            log.info("Anthropic key: OUT OF CREDIT (%s). Whoever owns the Anthropic Console must top up "
                     "(Plans & Billing).", failure.detail)
        else:
            log.info("Anthropic key: NOT WORKING (%s: %s)", failure.code, failure.detail)

    total = db.total_spend()
    budget = settings.claude_budget_total_usd
    log.info("\nClaude spend recorded by this project: $%.4f of the app's $%.2f budget ($%.4f left in the app's "
             "budget; the Anthropic account's own balance is only visible in the Console)",
             total, budget, max(budget - total, 0))
    log.info("\n%-12s %-28s %8s %10s  %s", "source", "model", "calls", "cost $", "last call")
    for row in db.spend_summary():
        log.info("%-12s %-28s %8d %10.4f  %s", row["source"], row["model"][:28], row["entries"], row["cost_usd"],
                 row["last_at"].strftime("%Y-%m-%d %H:%M"))
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
