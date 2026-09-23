"""Apply database migrations in order.

Uses SUPABASE_ADMIN_DSN (the postgres user) — run this on your laptop only.
Each file in db/migrations/ is applied once, inside a transaction, and
recorded in lead_agent.schema_migrations. Re-running is safe.

    .venv/Scripts/python scripts/migrate.py
"""

import logging
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.scripts.migrate")
ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"


def main() -> int:
    dsn = (dotenv_values(ROOT / ".env").get("SUPABASE_ADMIN_DSN") or "").strip()
    if not dsn:
        log.error("ERROR: SUPABASE_ADMIN_DSN is empty in .env")
        return 1

    with psycopg.connect(dsn, prepare_threshold=None, autocommit=True) as conn:
        role = conn.execute("select 1 from pg_roles where rolname = 'lead_agent_app'").fetchone()
        if role is None:
            log.error("ERROR: role lead_agent_app doesn't exist. Run scripts/create_app_role.py first.")
            return 1
        conn.execute("create schema if not exists lead_agent")
        conn.execute(
            "create table if not exists lead_agent.schema_migrations ("
            " filename text primary key, applied_at timestamptz not null default now())"
        )
        applied = {row[0] for row in conn.execute("select filename from lead_agent.schema_migrations")}

        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                log.info(f"  skip  {path.name} (already applied)")
                continue
            sql_text = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql_text)
                conn.execute("insert into lead_agent.schema_migrations (filename) values (%s)", (path.name,))
            log.info(f"  apply {path.name}")
    log.info("migrations up to date")
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
