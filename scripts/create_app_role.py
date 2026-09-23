"""Create the least-privilege `lead_agent_app` database user (specs.md §6).

What it does, in order:
  1. Reads SUPABASE_ADMIN_DSN from .env (the postgres user; laptop-only).
  2. Creates the `lead_agent_app` login role with a freshly generated password,
     unless it already exists (re-running is safe; see --rotate).
  3. Runs db/migrations/0000_app_role.sql (schema + grants).
  4. Builds the app's connection string (same host, Session pooler port 5432,
     user `lead_agent_app.<project-ref>`) and writes it to .env as
     SUPABASE_DB_DSN. The password is never printed.
  5. Connects as the new user and proves what it can and can't do.

Usage (from the lead-agent folder):
    .venv/Scripts/python scripts/create_app_role.py            # create / verify
    .venv/Scripts/python scripts/create_app_role.py --rotate   # new password
"""

import argparse
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from psycopg import errors, sql

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
MIGRATION = ROOT / "db" / "migrations" / "0000_app_role.sql"
APP_ROLE = "lead_agent_app"
SESSION_POOLER_PORT = 5432


def fail(message: str) -> None:
    print(f"ERROR: {message}")
    sys.exit(1)


def build_app_dsn(admin_dsn: str, password: str) -> str:
    """Same host/database as the admin DSN, but the app user + Session pooler port."""
    parts = urlsplit(admin_dsn)
    admin_user = unquote(parts.username or "")
    # Supabase pooler usernames look like "postgres.<project-ref>"; keep the ref.
    _, dot, project_ref = admin_user.partition(".")
    app_user = f"{APP_ROLE}.{project_ref}" if dot else APP_ROLE

    host = parts.hostname or ""
    port = SESSION_POOLER_PORT if "pooler.supabase.com" in host else (parts.port or 5432)
    netloc = f"{quote(app_user, safe='.')}:{quote(password, safe='')}@{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def write_env_value(key: str, value: str) -> None:
    """Replace (or append) KEY=value in .env atomically, keeping every other line."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    new_line = f"{key}={value}\n"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    # Write to a temp file in the same folder, then swap it in, so a crash
    # mid-write can never leave a half-written .env.
    fd, tmp = tempfile.mkstemp(dir=ENV_PATH.parent, prefix=".env.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    os.replace(tmp, ENV_PATH)


def verify_app_user(app_dsn: str) -> bool:
    """Connect as the app user and check its powers match the spec."""
    ok = True
    with psycopg.connect(app_dsn, prepare_threshold=None, autocommit=True) as conn:
        user = conn.execute("select current_user").fetchone()[0]
        print(f"  connects as: {user}")
        if user != APP_ROLE:
            print("  FAIL: connected as the wrong user")
            ok = False

        checks = [
            ("can't create tables in lead_agent", "create table lead_agent._probe (id int)"),
            ("can't read Week 4's schema", "select 1 from content_agent.profiles limit 1"),
            ("can't create a schema", "create schema _probe_schema"),
        ]
        for label, statement in checks:
            try:
                conn.execute(statement)
                print(f"  FAIL: {label} (the statement succeeded)")
                ok = False
            except (errors.InsufficientPrivilege, errors.UndefinedTable, errors.InvalidSchemaName):
                # Permission denied, or the other schema doesn't exist: both mean no access.
                print(f"  ok: {label}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rotate", action="store_true", help="set a new password on an existing role")
    args = parser.parse_args()

    if not ENV_PATH.exists():
        fail(".env not found in the lead-agent folder")
    env = dotenv_values(ENV_PATH)
    admin_dsn = (env.get("SUPABASE_ADMIN_DSN") or "").strip()
    if not admin_dsn:
        fail("SUPABASE_ADMIN_DSN is empty in .env")
    if not unquote(urlsplit(admin_dsn).username or "").startswith("postgres"):
        fail("SUPABASE_ADMIN_DSN should log in as the postgres user")

    existing_app_dsn = (env.get("SUPABASE_DB_DSN") or "").strip()
    existing_user = unquote(urlsplit(existing_app_dsn).username or "") if existing_app_dsn else ""
    env_has_app_dsn = existing_user.startswith(APP_ROLE)

    password = None
    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        role_exists = conn.execute(
            "select 1 from pg_roles where rolname = %s", (APP_ROLE,)
        ).fetchone() is not None

        if not role_exists:
            password = secrets.token_urlsafe(32)
            # DDL can't take bind parameters, so the password is composed as a
            # properly escaped SQL literal on the client side.
            conn.execute(
                sql.SQL(
                    "create role {} with login password {} nosuperuser nocreatedb "
                    "nocreaterole noinherit noreplication nobypassrls connection limit 20"
                ).format(sql.Identifier(APP_ROLE), sql.Literal(password))
            )
            print(f"created role {APP_ROLE}")
        elif args.rotate or not env_has_app_dsn:
            if not args.rotate:
                fail(
                    f"role {APP_ROLE} already exists but .env has no DSN for it. "
                    "Re-run with --rotate to set a new password."
                )
            password = secrets.token_urlsafe(32)
            conn.execute(
                sql.SQL("alter role {} with password {}").format(
                    sql.Identifier(APP_ROLE), sql.Literal(password)
                )
            )
            print(f"rotated password for {APP_ROLE}")
        else:
            print(f"role {APP_ROLE} already exists and .env already has its DSN; keeping it")

        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        print(f"applied {MIGRATION.relative_to(ROOT)}")

    if password is not None:
        write_env_value("SUPABASE_DB_DSN", build_app_dsn(admin_dsn, password))
        print("wrote SUPABASE_DB_DSN to .env (password not shown)")
        app_dsn = dotenv_values(ENV_PATH)["SUPABASE_DB_DSN"]
    else:
        app_dsn = existing_app_dsn

    print("verifying the app user:")
    if not verify_app_user(app_dsn):
        fail("app user verification failed (see above)")
    print("done: the app user is least-privilege as specified")


if __name__ == "__main__":
    main()
