"""Data access layer against the real database (throwaway rows, cleaned up with the admin DSN)."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app import db
from app.config import DEV_LIMITS

pytestmark = pytest.mark.db


@pytest.fixture
def run_row(app_dsn, admin_dsn):
    run, created = db.create_run(
        idempotency_key=f"test-{uuid.uuid4()}", objective="db layer test objective",
        objective_hash="h-" + uuid.uuid4().hex, limits=DEV_LIMITS.to_dict(), run_kind="dev",
    )
    assert created
    yield run
    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        for table in ("tool_calls", "leads"):
            conn.execute(f"delete from lead_agent.{table} where run_id = %s", (run["id"],))
        conn.execute("delete from lead_agent.spend_ledger where ref_id = %s", (run["id"],))
        conn.execute("delete from lead_agent.runs where id = %s", (run["id"],))


def test_idempotent_create_returns_existing(run_row):
    again, created = db.create_run(
        idempotency_key=run_row["idempotency_key"], objective="different text", objective_hash="x",
        limits={}, run_kind="dev",
    )
    assert not created and again["id"] == run_row["id"]


def test_reserve_usage_never_exceeds_limit_even_when_racing(run_row):
    run_id = str(run_row["id"])
    max_scrapes = DEV_LIMITS.max_scrapes  # 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: db.reserve_usage(run_id, "scrapes", "max_scrapes"), range(10)))
    granted = [r for r in results if r is not None]
    assert len(granted) == max_scrapes
    assert db.get_run(run_id)["usage"]["scrapes"] == max_scrapes


def test_tool_call_lifecycle_and_seq(run_row):
    run_id = str(run_row["id"])
    s1, s2 = db.next_tool_call_seq(run_id), db.next_tool_call_seq(run_id)
    assert (s1, s2) == (1, 2)
    call_id = db.insert_tool_call(run_id, s1, "orchestrator", "discover_companies", "initial pool", "q=saas")
    db.finish_tool_call(call_id, "success", "3 companies", duration_ms=120)
    calls = db.list_tool_calls(run_id)
    assert calls[0]["status"] == "success" and calls[0]["result_summary"] == "3 companies"


def test_lead_dedupe_and_fetched_urls(run_row):
    run_id = str(run_row["id"])
    kwargs = dict(company_name="Acme", company_domain="acme-test.io", linkedin_url=None, discovery_data={"a": 1},
                  prescreen_result="passed", prescreen_reason="ok", qualification_status="pending",
                  fetched_urls=["https://acme-test.io/"])
    lead = db.insert_lead_if_new(run_id, **kwargs)
    assert lead is not None
    assert db.insert_lead_if_new(run_id, **kwargs) is None  # same domain, same run
    db.add_fetched_url(str(lead["id"]), "https://acme-test.io/about")
    db.add_fetched_url(str(lead["id"]), "https://acme-test.io/about")
    assert db.get_lead(str(lead["id"]))["fetched_urls"] == ["https://acme-test.io/", "https://acme-test.io/about"]
    db.update_lead(str(lead["id"]), qualification_status="qualified", confidence=0.8, fit_reasons=["x"])
    assert db.lead_counts(run_id) == {"qualified": 1}


def test_spend_ledger_totals(run_row):
    before = db.total_spend()
    db.record_spend("spike", 0.0123, ref_id=str(run_row["id"]), model="claude-haiku-4-5", note="test")
    assert float(db.total_spend() - before) == pytest.approx(0.0123, abs=1e-5)
