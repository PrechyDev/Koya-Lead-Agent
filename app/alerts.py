"""Owner alerts: in-app System issues + optional webhook (n8n → email). Never breaks the caller.

* raise_alert(...) records a row in lead_agent.system_events. The same open code within 30 minutes
  bumps `occurrences` instead of creating a new row, so a broken key doesn't flood your inbox.
* A new (or re-opened) issue is POSTed to ALERT_WEBHOOK_URL with header X-Alert-Secret.
* If the database itself is down, the alert goes straight to the webhook.
"""

import logging
import threading
from datetime import UTC, datetime

import httpx

from app import db
from app.config import get_settings
from app.failures import CATALOGUE

log = logging.getLogger("lead_agent.alerts")
DEDUPE_MINUTES = 30


def _post_webhook(payload: dict) -> bool:
    settings = get_settings()
    if not settings.alert_webhook_url:
        return False
    try:
        response = httpx.post(settings.alert_webhook_url, json=payload, timeout=8,
                              headers={"X-Alert-Secret": settings.alert_webhook_secret or ""})
        return response.status_code < 400
    except httpx.HTTPError:
        log.warning("alert webhook failed")
        return False


def _payload(code: str, message: str, run_id: str | None, severity: str, service: str, occurrences: int) -> dict:
    base = get_settings().app_base_url.rstrip("/")
    return {
        "app": "Koya Lead Research Agent",
        "severity": severity, "service": service, "code": code,
        "summary": f"[{severity.upper()}] {service}: {code.replace('_', ' ')}",
        "message": message, "occurrences": occurrences,
        "run_url": f"{base}/runs/{run_id}" if run_id else None,
        "issues_url": f"{base}/system",
        "at": datetime.now(UTC).isoformat(),
    }


def raise_alert(code: str, detail: str = "", run_id: str | None = None, notify: bool = True) -> None:
    """Record + notify. Safe to call from anywhere (sync). Never raises."""
    kind = CATALOGUE.get(code, CATALOGUE["unexpected"])
    message = kind.admin + (f" — Detail: {detail}" if detail else "")
    try:
        existing = db.fetch_one(
            f"""select id, occurrences from {db.t('system_events')}
                where code = %s and resolved_at is null and last_seen_at > now() - make_interval(mins => %s)
                order by last_seen_at desc limit 1""",
            (kind.code, DEDUPE_MINUTES),
        )
        if existing:
            db.execute(f"update {db.t('system_events')} set occurrences = occurrences + 1, last_seen_at = now(), "
                       f"message = %s, run_id = coalesce(%s, run_id) where id = %s",
                       (message[:2000], run_id, existing["id"]))
            return
        row = db.fetch_one(
            f"""insert into {db.t('system_events')} (severity, service, code, message, run_id)
                values (%s, %s, %s, %s, %s) returning id""",
            (kind.severity, kind.service, kind.code, message[:2000], run_id),
        )
    except Exception:  # noqa: BLE001 — the DB may be the thing that's down
        log.exception("could not record alert %s", code)
        if notify:
            threading.Thread(target=_post_webhook, args=(_payload(kind.code, message, run_id, kind.severity,
                                                                  kind.service, 1),), daemon=True).start()
        return
    if notify and kind.severity in ("warning", "critical"):
        def send():
            if _post_webhook(_payload(kind.code, message, run_id, kind.severity, kind.service, 1)):
                try:
                    db.execute(f"update {db.t('system_events')} set notified_at = now() where id = %s", (row["id"],))
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=send, daemon=True).start()


def check_budget() -> None:
    """Warn once when Claude spend passes 80% of the budget."""
    settings = get_settings()
    try:
        spent = db.total_spend()
    except Exception:  # noqa: BLE001
        return
    total = settings.claude_budget_total_usd
    if total and spent >= total * 8 / 10:
        raise_alert("budget_warning", f"${spent:.2f} of ${total:.2f} used")


def open_issue_count() -> int:
    try:
        row = db.fetch_one(f"select count(*) as n from {db.t('system_events')} where resolved_at is null "
                           f"and severity in ('warning', 'critical')")
        return int(row["n"])
    except Exception:  # noqa: BLE001
        return 0


def list_events(limit: int = 100) -> list[dict]:
    return db.fetch_all(
        f"""select e.*, m.full_name as resolved_by_name from {db.t('system_events')} e
            left join {db.t('members')} m on m.user_id = e.resolved_by
            order by (e.resolved_at is null) desc, e.last_seen_at desc limit %s""", (limit,))


def resolve(event_id: str, user_id: str) -> None:
    db.execute(f"update {db.t('system_events')} set resolved_at = now(), resolved_by = %s where id = %s",
               (user_id, event_id))
