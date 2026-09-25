"""Give the first people access (run once, on your laptop).

    .venv/Scripts/python scripts/bootstrap_owner.py you@example.com "Your Name" --owner
    .venv/Scripts/python scripts/bootstrap_owner.py grader@example.com "Grader Name"          # client admin

If the email already has an account in this Supabase project (e.g. from Week 3/4), it's added as a member
with NO email sent — they sign in with their existing password. Otherwise pass --invite to send a Supabase
invite email (they set a password via /accept-invite). Uses SUPABASE_ADMIN_DSN only to set the owner flag.
"""

import argparse
import logging
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.scripts.bootstrap_owner")
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    parser.add_argument("full_name")
    parser.add_argument("--owner", action="store_true", help="mark as the owner (can't be removed)")
    parser.add_argument("--member", action="store_true", help="add as a member instead of an admin")
    parser.add_argument("--invite", action="store_true", help="send an invite email if no account exists")
    args = parser.parse_args()

    if args.owner and args.member:  # checked BEFORE any invite is sent or account created
        log.error("--owner and --member can't be combined: the owner is always an admin.")
        return 1
    email = args.email.strip().lower()
    row = db.fetch_one(f"select {db.SCHEMA}.auth_user_id_by_email(%s) as id", (email,))
    user_id = str(row["id"]) if row and row["id"] else None
    if user_id:
        log.info(f"{email} already has an account in this project: adding membership (no email sent).")
    elif args.invite:
        user_id, _ = auth.invite_user(email, args.full_name, role="member" if args.member else "admin")
        if not user_id:
            log.error("Supabase didn't return a user id for %s; nothing was added. Try again.", email)
            return 1
        log.info(f"Invite email sent to {email}. They'll set a password at /accept-invite.")
    else:
        log.info(f"No account exists for {email}. Re-run with --invite to send an invite email.")
        return 1

    role = "member" if args.member else "admin"
    db.add_member(user_id, email, args.full_name, role, None)
    if args.owner:
        admin_dsn = (dotenv_values(ROOT / ".env").get("SUPABASE_ADMIN_DSN") or "").strip()
        if not admin_dsn:
            log.error("SUPABASE_ADMIN_DSN is empty in .env; it's needed (laptop only) to set the owner flag.")
            return 1
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            conn.execute(f"update {db.SCHEMA}.members set is_owner = true, role = 'admin', is_developer = true "
                         "where user_id = %s",
                         (user_id,))
    log.info(f"Done: {args.full_name} is {'the owner and ' if args.owner else ''}{role}.")
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
