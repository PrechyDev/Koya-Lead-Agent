"""Data access layer: every SQL statement in the app lives here (specs.md §6).

* Connects as the least-privilege `lead_agent_app` role (SUPABASE_DB_DSN).
* Every table name is schema-qualified via SCHEMA (never relies on search_path).
* `prepare_threshold=None` because Supabase's pooler can't keep prepared statements.
* Uses psycopg's synchronous pool. Async code calls it through `run()`, which
  hops to a worker thread: this works identically on Windows and Linux.
* Transient connection errors are retried 3 times with backoff (E-25).
"""

import asyncio
import json
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.config import get_settings

T = TypeVar("T")
SCHEMA = get_settings().supabase_db_schema
_pool: ConnectionPool | None = None

RESEARCHED_STATUSES = ("qualified", "not_qualified", "needs_review")
TERMINAL_STATUSES = ("completed", "completed_partial", "failed", "cancelled", "superseded",
                     "needs_clarification")
ACTIVE_STATUSES = ("queued", "refining_icp", "discovering", "researching", "drafting", "finalizing")

RUN_COLUMNS = {
    "icp", "icp_assumptions", "icp_signature", "clarification_question", "duplicate_of_run_id",
    "repeat_choice", "cross_run_dedupe", "usage", "models", "status", "status_detail", "error_message",
    "summary", "shortfall_reason", "quality_scorecard", "cost_usd", "num_turns", "started_at", "finished_at",
    "request_type", "error_detail",
}
LEAD_COLUMNS = {
    "company_name", "linkedin_url", "discovery_data", "prescreen_result", "prescreen_reason",
    "qualification_status", "confidence", "hard_filter_checks", "fit_reasons", "concerns", "source_urls",
    "source_summary", "email_sequence", "linkedin_message", "outreach_status", "outreach_attempts",
    "grounding_report", "review_status", "reviewer_note", "reviewed_by", "reviewed_at", "fetched_urls",
    "disqualifier_checks", "soft_preference_checks", "tools_detected", "confidence_breakdown",
}
MEMBER_COLUMNS = {"full_name", "role", "is_active"}
JSON_COLUMNS = {
    "icp", "icp_assumptions", "usage", "models", "quality_scorecard", "discovery_data", "hard_filter_checks",
    "fit_reasons", "concerns", "email_sequence", "grounding_report", "injection_flags", "expected", "actual",
    "disqualifier_checks", "soft_preference_checks", "tools_detected", "confidence_breakdown",
}


def t(name: str) -> str:
    """Schema-qualified table name, e.g. t('runs') -> 'lead_agent.runs'."""
    return f"{SCHEMA}.{name}"


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = get_settings().supabase_db_dsn
        if not dsn:
            raise RuntimeError("SUPABASE_DB_DSN is not set")
        _pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=8,
            kwargs={"prepare_threshold": None, "row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _retry(fn: Callable[[], T]) -> T:
    delay = 0.5
    for attempt in range(3):
        try:
            return fn()
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if attempt == 2:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def _adapt(column: str, value: Any) -> Any:
    if column in JSON_COLUMNS and value is not None and not isinstance(value, Jsonb):
        return Jsonb(value)
    return value


def fetch_one(sql: str, params: tuple | dict = ()) -> dict | None:
    def go():
        with get_pool().connection() as conn:
            return conn.execute(sql, params).fetchone()
    return _retry(go)


def fetch_all(sql: str, params: tuple | dict = ()) -> list[dict]:
    def go():
        with get_pool().connection() as conn:
            return conn.execute(sql, params).fetchall()
    return _retry(go)


def execute(sql: str, params: tuple | dict = ()) -> None:
    def go():
        with get_pool().connection() as conn:
            conn.execute(sql, params)
    _retry(go)


async def run(fn: Callable[..., T], *args, **kwargs) -> T:
    """Call a blocking DB function from async code without blocking the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def create_run(
    *, idempotency_key: str, objective: str, objective_hash: str, limits: dict, run_kind: str = "app",
    created_by: str | None = None, parent_run_id: str | None = None, models: dict | None = None,
) -> tuple[dict, bool]:
    """Insert a run. Returns (run, created). A repeated idempotency key returns the existing run (E-20)."""
    row = fetch_one(
        f"""insert into {t('runs')} (idempotency_key, objective, objective_hash, limits, run_kind,
                                   created_by, parent_run_id, models)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (idempotency_key) do nothing
            returning *""",
        (idempotency_key, objective.strip(), objective_hash, Jsonb(limits), run_kind, created_by,
         parent_run_id, Jsonb(models or {})),
    )
    if row:
        return row, True
    return fetch_one(f"select * from {t('runs')} where idempotency_key = %s", (idempotency_key,)), False


def get_run(run_id: str) -> dict | None:
    return fetch_one(f"select * from {t('runs')} where id = %s", (run_id,))


# Run history filters (the Runs page). eval_record runs are internal and never listed.
RUN_STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "in_progress": ("queued", "refining_icp", "discovering", "researching", "drafting", "finalizing"),
    "needs_you": ("awaiting_confirmation", "needs_clarification"),
    "completed": ("completed",),
    "partial": ("completed_partial",),
    "failed": ("failed",),
    "cancelled": ("cancelled", "superseded"),
}


def _run_filters(created_by: str | None, q: str) -> tuple[str, list]:
    where, params = ["r.run_kind <> 'eval_record'"], []
    if created_by:
        where.append("r.created_by = %s")
        params.append(created_by)
    if q:
        # ILIKE with the user's text as a literal: escape the LIKE wildcards % and _.
        where.append(r"r.objective ilike %s escape '\'")
        params.append("%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%")
    return " and ".join(where), params


def search_runs(*, group: str = "", created_by: str | None = None, q: str = "", limit: int = 20,
                offset: int = 0) -> tuple[list[dict], int, dict[str, int]]:
    """(page of runs, total matching, count per status group) for the Runs page."""
    base, params = _run_filters(created_by, q)
    counts_rows = fetch_all(f"select r.status, count(*) as n from {t('runs')} r where {base} group by r.status", params)
    by_status = {row["status"]: int(row["n"]) for row in counts_rows}
    counts = {g: sum(by_status.get(s, 0) for s in statuses) for g, statuses in RUN_STATUS_GROUPS.items()}
    counts["all"] = sum(by_status.values())
    where, page_params = base, list(params)
    if group in RUN_STATUS_GROUPS:
        where += " and r.status = any(%s)"
        page_params.append(list(RUN_STATUS_GROUPS[group]))
    total = counts.get(group, counts["all"]) if group in RUN_STATUS_GROUPS else counts["all"]
    rows = fetch_all(
        f"""select r.*, m.full_name as created_by_name
            from {t('runs')} r left join {t('members')} m on m.user_id = r.created_by
            where {where} order by r.created_at desc limit %s offset %s""",
        page_params + [limit, offset],
    )
    return rows, total, counts


def list_runs(limit: int = 50) -> list[dict]:
    return fetch_all(
        f"""select r.*, m.full_name as created_by_name
            from {t('runs')} r left join {t('members')} m on m.user_id = r.created_by
            order by r.created_at desc limit %s""",
        (limit,),
    )


def update_run(run_id: str, **fields: Any) -> dict | None:
    unknown = set(fields) - RUN_COLUMNS
    if unknown:
        raise ValueError(f"not updatable: {unknown}")
    if not fields:
        return get_run(run_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    return fetch_one(
        f"update {t('runs')} set {sets} where id = %s returning *",
        (*[_adapt(k, v) for k, v in fields.items()], run_id),
    )


def set_status(run_id: str, status: str, detail: str | None = None, **extra: Any) -> dict | None:
    fields: dict[str, Any] = {"status": status, **extra}
    if detail is not None:
        fields["status_detail"] = detail
    if status in TERMINAL_STATUSES and "finished_at" not in fields:
        fields["finished_at"] = datetime.now().astimezone()
    return update_run(run_id, **fields)


def reserve_usage(run_id: str, counter: str, limit_key: str, amount: int = 1) -> dict | None:
    """Atomically add `amount` to usage[counter] only if it stays within limits[limit_key] (E-32).

    Returns the updated usage, or None if the limit would be exceeded. One SQL
    statement, so two concurrent callers can never both take the last slot.
    """
    row = fetch_one(
        f"""update {t('runs')}
            set usage = jsonb_set(usage, %s::text[],
                                  to_jsonb(coalesce((usage->>%s)::int, 0) + %s))
            where id = %s
              and coalesce((usage->>%s)::int, 0) + %s <= (limits->>%s)::int
            returning usage""",
        ([counter], counter, amount, run_id, counter, amount, limit_key),
    )
    return row["usage"] if row else None


def add_usage(run_id: str, counter: str, amount: int = 1) -> dict:
    row = fetch_one(
        f"""update {t('runs')}
            set usage = jsonb_set(usage, %s::text[], to_jsonb(coalesce((usage->>%s)::int, 0) + %s))
            where id = %s returning usage""",
        ([counter], counter, amount, run_id),
    )
    return row["usage"] if row else {}


def append_usage_item(run_id: str, key: str, value: str) -> None:
    """Append to a list inside usage in ONE statement (a read-modify-write of the whole usage object could
    overwrite a counter that a parallel researcher had just reserved)."""
    execute(
        f"""update {t('runs')}
            set usage = jsonb_set(usage, %s::text[], coalesce(usage->%s, '[]'::jsonb) || to_jsonb(%s::text))
            where id = %s""",
        ([key], key, value, run_id),
    )


def set_target_qualified(run_id: str, target: int) -> dict:
    """The one limit that may change after a run is created: the lead target, set from the objective by
    save_icp (D-57). Everything else in `limits` is fixed when the run is created."""
    row = fetch_one(
        f"""update {t('runs')} set limits = jsonb_set(limits, '{{target_qualified}}', to_jsonb(%s::int))
            where id = %s returning limits""",
        (int(target), run_id),
    )
    return row["limits"] if row else {}


def next_tool_call_seq(run_id: str) -> int:
    row = fetch_one(
        f"update {t('runs')} set tool_call_count = tool_call_count + 1 where id = %s returning tool_call_count",
        (run_id,),
    )
    return int(row["tool_call_count"]) if row else 0


def active_run() -> dict | None:
    return fetch_one(
        f"select * from {t('runs')} where status = any(%s) order by created_at desc limit 1",
        (list(ACTIVE_STATUSES),),
    )


def count_full_runs_today(created_by: str | None = None) -> int:
    """Runs that went past the ICP phase today (a superseded/clarification run is cheap and not counted)."""
    sql = (f"select count(*) as n from {t('runs')} where created_at >= date_trunc('day', now())"
           f" and run_kind = 'app' and status not in ('superseded', 'needs_clarification', 'cancelled')")
    params: tuple = ()
    if created_by:
        sql += " and created_by = %s"
        params = (created_by,)
    return int(fetch_one(sql, params)["n"])


def find_recent_run_by_hash(  # only runs that produced qualified leads are worth pointing to
objective_hash: str, days: int, exclude_id: str | None = None) -> dict | None:
    return fetch_one(
        f"""select * from {t('runs')}
            where objective_hash = %s and created_at > now() - make_interval(days => %s)
              and status in ('completed', 'completed_partial') and (%s::uuid is null or id <> %s::uuid)
              and coalesce((usage->>'qualified')::int, 0) > 0
            order by created_at desc limit 1""",
        (objective_hash, days, exclude_id, exclude_id),
    )


def find_recent_run_by_signature(signature: str, days: int, exclude_id: str) -> dict | None:
    return fetch_one(
        f"""select * from {t('runs')}
            where icp_signature = %s and created_at > now() - make_interval(days => %s)
              and status in ('completed', 'completed_partial') and id <> %s
              and coalesce((usage->>'qualified')::int, 0) > 0
            order by created_at desc limit 1""",
        (signature, days, exclude_id),
    )


def fail_orphaned_runs() -> int:
    """On boot: any run still 'active' was interrupted by a restart (E-19)."""
    rows = fetch_all(
        f"""update {t('runs')} set status = 'failed', finished_at = now(),
                   error_message = coalesce(error_message, 'Interrupted by server restart'),
                   status_detail = 'Interrupted by server restart; work saved so far is kept'
            where status = any(%s) returning id""",
        (list(ACTIVE_STATUSES),),
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------
def insert_tool_call(run_id: str, seq: int, agent_role: str, tool_name: str, purpose: str | None,
                     input_summary: str | None, status: str = "running") -> str:
    row = fetch_one(
        f"""insert into {t('tool_calls')} (run_id, seq, agent_role, tool_name, purpose, input_summary, status)
            values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (run_id, seq, agent_role, tool_name, (purpose or "")[:500], (input_summary or "")[:1000], status),
    )
    return str(row["id"])


def finish_tool_call(call_id: str, status: str, result_summary: str | None = None,
                     error_message: str | None = None, duration_ms: int | None = None,
                     external_cost_usd: float | None = None) -> None:
    execute(
        f"""update {t('tool_calls')} set status = %s, result_summary = %s, error_message = %s,
                   duration_ms = %s, external_cost_usd = %s, finished_at = now() where id = %s""",
        (status, (result_summary or "")[:2000], (error_message or None) and error_message[:1000],
         duration_ms, external_cost_usd, call_id),
    )


def list_tool_calls(run_id: str) -> list[dict]:
    return fetch_all(f"select * from {t('tool_calls')} where run_id = %s order by seq", (run_id,))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
def insert_lead_if_new(run_id: str, *, company_name: str, company_domain: str, linkedin_url: str | None,
                       discovery_data: dict, prescreen_result: str, prescreen_reason: str,
                       qualification_status: str, fetched_urls: list[str]) -> dict | None:
    """Insert a discovered company; returns None if this run already has that domain (E-10)."""
    return fetch_one(
        f"""insert into {t('leads')} (run_id, company_name, company_domain, linkedin_url, discovery_data,
                                    prescreen_result, prescreen_reason, qualification_status, fetched_urls)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id, company_domain) do nothing returning *""",
        (run_id, company_name, company_domain, linkedin_url, Jsonb(discovery_data), prescreen_result,
         prescreen_reason, qualification_status, fetched_urls),
    )


def get_lead(lead_id: str) -> dict | None:
    return fetch_one(
        f"""select l.*, m.full_name as reviewed_by_name from {t('leads')} l
            left join {t('members')} m on m.user_id = l.reviewed_by where l.id = %s""",
        (lead_id,),
    )


def get_lead_by_domain(run_id: str, domain: str) -> dict | None:
    return fetch_one(f"select * from {t('leads')} where run_id = %s and company_domain = %s", (run_id, domain))


def list_leads(run_id: str) -> list[dict]:
    return fetch_all(
        f"""select l.*, m.full_name as reviewed_by_name from {t('leads')} l
            left join {t('members')} m on m.user_id = l.reviewed_by
            where l.run_id = %s
            order by case l.qualification_status when 'qualified' then 0 when 'needs_review' then 1
                     when 'pending' then 2 else 3 end, l.confidence desc nulls last, l.created_at""",
        (run_id,),
    )


def update_lead(lead_id: str, **fields: Any) -> dict | None:
    unknown = set(fields) - LEAD_COLUMNS
    if unknown:
        raise ValueError(f"not updatable: {unknown}")
    sets = ", ".join(f"{k} = %s" for k in fields)
    return fetch_one(
        f"update {t('leads')} set {sets} where id = %s returning *",
        (*[_adapt(k, v) for k, v in fields.items()], lead_id),
    )


def add_fetched_url(lead_id: str, url: str) -> None:
    execute(
        f"""update {t('leads')} set fetched_urls = array_append(fetched_urls, %s)
            where id = %s and not (%s = any(fetched_urls))""",
        (url, lead_id, url),
    )


def recently_researched(domains: list[str], days: int, exclude_run_id: str) -> dict[str, str]:
    """domain -> run_id for domains researched in another run within `days` (E-38)."""
    if not domains:
        return {}
    rows = fetch_all(
        f"""select distinct on (company_domain) company_domain, run_id from {t('leads')}
            where company_domain = any(%s) and run_id <> %s
              and qualification_status = any(%s) and created_at > now() - make_interval(days => %s)
            order by company_domain, created_at desc""",
        (domains, exclude_run_id, list(RESEARCHED_STATUSES), days),
    )
    return {r["company_domain"]: str(r["run_id"]) for r in rows}


def lead_counts(run_id: str) -> dict[str, int]:
    rows = fetch_all(
        f"select qualification_status as s, count(*) as n from {t('leads')} where run_id = %s group by 1",
        (run_id,),
    )
    return {r["s"]: int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# Scrape cache
# ---------------------------------------------------------------------------
def get_cached_page(url: str, max_age_days: int) -> dict | None:
    return fetch_one(
        f"select * from {t('scrape_cache')} where url = %s and fetched_at > now() - make_interval(days => %s)",
        (url, max_age_days),
    )


def put_cached_page(*, url: str, domain: str, final_url: str | None, title: str | None, content: str,
                    truncated: bool, status_code: int | None, injection_flags: list[str],
                    tools_detected: list[dict] | None = None) -> None:
    execute(
        f"""insert into {t('scrape_cache')} (url, domain, final_url, title, content, truncated, status_code,
                                           injection_flags, tools_detected, fetched_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            on conflict (url) do update set final_url = excluded.final_url, title = excluded.title,
              content = excluded.content, truncated = excluded.truncated, status_code = excluded.status_code,
              injection_flags = excluded.injection_flags, tools_detected = excluded.tools_detected,
              fetched_at = now()""",
        (url, domain, final_url, title, content, truncated, status_code, Jsonb(injection_flags),
         Jsonb(tools_detected or [])),
    )


# ---------------------------------------------------------------------------
# Spend ledger
# ---------------------------------------------------------------------------
def record_spend(source: str, cost_usd: Decimal | float, *, ref_id: str | None = None, model: str | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None, note: str | None = None) -> None:
    execute(
        f"""insert into {t('spend_ledger')} (source, ref_id, model, input_tokens, output_tokens, cost_usd, note)
            values (%s, %s, %s, %s, %s, %s, %s)""",
        (source, ref_id, model, input_tokens, output_tokens, Decimal(str(cost_usd)), note),
    )


def total_spend() -> Decimal:
    row = fetch_one(f"select coalesce(sum(cost_usd), 0) as s from {t('spend_ledger')}")
    return Decimal(str(row["s"]))


def spend_summary() -> list[dict]:
    return fetch_all(f"select * from {t('spend_summary_v')} order by cost_usd desc")


def spend_by_run(limit: int = 50) -> list[dict]:
    return fetch_all(
        f"""select r.id, r.objective, r.status, r.created_at, r.cost_usd, r.models, m.full_name as created_by_name
            from {t('runs')} r left join {t('members')} m on m.user_id = r.created_by
            order by r.created_at desc limit %s""",
        (limit,),
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
def get_member(user_id: str) -> dict | None:
    return fetch_one(f"select * from {t('members')} where user_id = %s", (user_id,))


def get_member_by_email(email: str) -> dict | None:
    return fetch_one(f"select * from {t('members')} where lower(email) = lower(%s)", (email,))


def list_members() -> list[dict]:
    return fetch_all(f"select * from {t('members')} order by is_owner desc, role, full_name")


def add_member(user_id: str, email: str, full_name: str, role: str, invited_by: str | None) -> dict | None:
    return fetch_one(
        f"""insert into {t('members')} (user_id, email, full_name, role, invited_by)
            values (%s, %s, %s, %s, %s)
            on conflict (user_id) do update set is_active = true
            returning *""",
        (user_id, email.lower(), full_name, role, invited_by),
    )


def update_member(user_id: str, **fields: Any) -> dict:
    unknown = set(fields) - MEMBER_COLUMNS
    if unknown:
        raise ValueError(f"not updatable: {unknown}")
    sets = ", ".join(f"{k} = %s" for k in fields)
    return fetch_one(f"update {t('members')} set {sets} where user_id = %s returning *",
                     (*fields.values(), user_id))


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------
def insert_eval_result(**row: Any) -> None:
    cols = ["eval_name", "stage", "model", "repeat_no", "case_id", "expected", "actual", "passed", "score",
            "cost_usd", "latency_ms", "notes"]
    execute(
        f"insert into {t('eval_results')} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
        tuple(_adapt(c, row.get(c)) for c in cols),
    )


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False)
