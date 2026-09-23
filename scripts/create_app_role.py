"""Create the least-privilege `lead_agent_app` database user (specs.md §6).

This script only READS .env; it never writes to it or prints a password. You own the secret:

  1. Generate a password yourself:
         .venv/Scripts/python -c "import secrets; print(secrets.token_urlsafe(32))"
  2. Put the full connection string in .env (Supabase → Connect → Session pooler, then swap in the app user):
         SUPABASE_DB_DSN=postgresql://lead_agent_app.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
  3. Run this script. With SUPABASE_ADMIN_DSN (the postgres user; laptop only) it:
       - creates the `lead_agent_app` login role with the password from SUPABASE_DB_DSN, or, if the role
         exists, sets its password to match (so rotating = change the password in .env, re-run);
       - runs db/migrations/0000_app_role.sql (schema + grants);
       - connects as the app user and proves what it can and can't do.

Usage (from the lead-agent folder):
    .venv/Scripts/python scripts/create_app_role.py
"""

import argparse
import logging
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg
from dotenv import dotenv_values
from psycopg import errors, sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.scripts.create_app_role")
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
MIGRATION = ROOT / "db" / "migrations" / "0000_app_role.sql"
APP_ROLE = "lead_agent_app"
MIN_PASSWORD_LENGTH = 24


def fail(message: str) -> None:
    log.error(f"ERROR: {message}")
    sys.exit(1)


def dsn_template(admin_dsn: str) -> str:
    """What SUPABASE_DB_DSN should look like, built from the admin DSN's host (no secrets in it)."""
    parts = urlsplit(admin_dsn)
    _, dot, project_ref = unquote(parts.username or "").partition(".")
    user = f"{APP_ROLE}.{project_ref}" if dot else APP_ROLE
    return f"postgresql://{user}:<your-password>@{parts.hostname}:5432{parts.path or '/postgres'}"


def app_credentials(app_dsn: str, admin_dsn: str) -> str:
    """Check SUPABASE_DB_DSN is for the app user and has a strong password; return the password."""
    parts = urlsplit(app_dsn)
    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if not user.split(".")[0] == APP_ROLE:
        fail(f"SUPABASE_DB_DSN must log in as {APP_ROLE}, not '{user.split('.')[0]}'. Expected:\n  "
             + dsn_template(admin_dsn))
    if len(password) < MIN_PASSWORD_LENGTH or password.startswith("<"):
        fail(f"SUPABASE_DB_DSN needs a real password of at least {MIN_PASSWORD_LENGTH} characters. Generate one with:\n"
             '  .venv/Scripts/python -c "import secrets; print(secrets.token_urlsafe(32))"')
    if urlsplit(admin_dsn).hostname != parts.hostname:
        fail("SUPABASE_DB_DSN and SUPABASE_ADMIN_DSN point at different hosts. Expected:\n  " + dsn_template(admin_dsn))
    return password


def verify_app_user(app_dsn: str) -> bool:
    """Connect as the app user and check its powers match the spec."""
    ok = True
    with psycopg.connect(app_dsn, prepare_threshold=None, autocommit=True) as conn:
        user = conn.execute("select current_user").fetchone()[0]
        log.info(f"  connects as: {user}")
        if user != APP_ROLE:
            log.error("  FAIL: connected as the wrong user")
            ok = False

        checks = [
            ("can't create tables in lead_agent", "create table lead_agent._probe (id int)"),
            ("can't read Week 4's schema", "select 1 from content_agent.profiles limit 1"),
            ("can't create a schema", "create schema _probe_schema"),
        ]
        for label, statement in checks:
            try:
                conn.execute(statement)
                log.error(f"  FAIL: {label} (the statement succeeded)")
                ok = False
            except (errors.InsufficientPrivilege, errors.UndefinedTable, errors.InvalidSchemaName):
                # Permission denied, or the other schema doesn't exist: both mean no access.
                log.info(f"  ok: {label}")
    return ok


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()

    if not ENV_PATH.exists():
        fail(".env not found in the lead-agent folder")
    env = dotenv_values(ENV_PATH)
    admin_dsn = (env.get("SUPABASE_ADMIN_DSN") or "").strip()
    if not admin_dsn:
        fail("SUPABASE_ADMIN_DSN is empty in .env")
    if not unquote(urlsplit(admin_dsn).username or "").startswith("postgres"):
        fail("SUPABASE_ADMIN_DSN should log in as the postgres user")
    app_dsn = (env.get("SUPABASE_DB_DSN") or "").strip()
    if not app_dsn:
        fail("SUPABASE_DB_DSN is empty. Add it to .env first, in this form:\n  " + dsn_template(admin_dsn))
    password = app_credentials(app_dsn, admin_dsn)

    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        role_exists = conn.execute("select 1 from pg_roles where rolname = %s", (APP_ROLE,)).fetchone() is not None
        # DDL can't take bind parameters, so the password is composed as an escaped SQL literal client-side.
        if not role_exists:
            conn.execute(
                sql.SQL(
                    "create role {} with login password {} nosuperuser nocreatedb "
                    "nocreaterole noinherit noreplication nobypassrls connection limit 20"
                ).format(sql.Identifier(APP_ROLE), sql.Literal(password))
            )
            log.info(f"created role {APP_ROLE} with the password from SUPABASE_DB_DSN")
        else:
            conn.execute(sql.SQL("alter role {} with password {}").format(sql.Identifier(APP_ROLE),
                                                                            sql.Literal(password)))
            log.info(f"role {APP_ROLE} exists; its password now matches SUPABASE_DB_DSN")

        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        log.info(f"applied {MIGRATION.relative_to(ROOT)}")

    log.info("verifying the app user:")
    if not verify_app_user(app_dsn):
        fail("app user verification failed (see above)")
    log.info("done: the app user is least-privilege as specified")


if __name__ == "__main__":
    configure_logging(cli=True)
    main()
