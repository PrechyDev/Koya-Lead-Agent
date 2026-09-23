"""Recover a session's real cost from Claude Code transcripts (and optionally record it).

    .venv/Scripts/python scripts/transcript_cost.py <session-id> [--record <run_id>]
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.transcripts import session_cost  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.scripts.transcript_cost")
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--record", help="run_id to record the recovered cost against")
    args = parser.parse_args()
    costs = session_cost(args.session_id)
    for model, cost in costs.items():
        log.info(f"{model}: ${cost:.4f}")
    log.info(f"TOTAL: ${sum(costs.values()):.4f}")
    if args.record:
        from app import db
        for model, cost in costs.items():
            if cost > 0:
                db.record_spend("run", cost, ref_id=args.record, model=model,
                                note=f"recovered from transcript {args.session_id} (run was killed before reporting)")
        log.info("recorded in spend_ledger")
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
