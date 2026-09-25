import os
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tests never touch paid services; make sure nothing picks up real keys by accident.
os.environ.setdefault("FIXTURE_MODE", "false")

_env = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    dsn = (_env.get("SUPABASE_ADMIN_DSN") or "").strip()
    if not dsn:
        pytest.skip("SUPABASE_ADMIN_DSN not set")
    return dsn


@pytest.fixture(scope="session")
def app_dsn() -> str:
    dsn = (_env.get("SUPABASE_DB_DSN") or "").strip()
    if not dsn:
        pytest.skip("SUPABASE_DB_DSN not set")
    return dsn


@pytest.fixture(autouse=True)
def no_real_triage(monkeypatch):
    """Tests never call the real candidate-triage model (it spends); a test that wants triage replaces this."""
    from app.services import triage

    async def none(target, hard_filters, companies):
        return triage.TriageResult()
    monkeypatch.setattr(triage, "triage_candidates", none)


@pytest.fixture(autouse=True)
def runs_today_start_at_zero(monkeypatch):
    """The database is shared with real use: today's real runs must not trip the daily limits inside tests."""
    from app import db
    monkeypatch.setattr(db, "count_full_runs_today", lambda created_by=None: 0)
