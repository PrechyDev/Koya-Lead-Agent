"""RunManager: starts runs in the background, one at a time (specs.md §5, §11; E-19 to E-21, E-30).

* Runs execute as asyncio tasks inside the web process (a run takes minutes).
* Only one run is active at a time (cost + Render free's 512 MB).
* Cancel = cancel the task; the SDK tears down the Claude Code subprocess. A person's Cancel ends the run
  `cancelled`; a server shutdown ends it `failed: Interrupted by server restart` (E-19).
* While a run is active, a keep-alive task pings our own public /health URL
  every 5 minutes so Render free doesn't put the service to sleep mid-run.
* A heartbeat touches each active run every minute. On boot, and before a new run starts, runs that went
  silent for 5 minutes are marked failed (work kept); a run another server is still working on is left alone,
  because the database is shared (a laptop and Render, D-99).
"""

import asyncio
import logging

import httpx

from app import db
from app.agent.runner import execute_run
from app.config import get_settings

log = logging.getLogger("lead_agent.runs")
KEEPALIVE_SECONDS = 300
HEARTBEAT_SECONDS = 60
SHUTDOWN_GRACE_S = 5  # how long a stopping server waits for runs to record that they were interrupted


class RunManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._user_cancels: set[str] = set()  # runs a person cancelled (vs. stopped by a shutdown)
        self._keepalive: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    def start(self, run_id: str, skip_icp: bool = False) -> None:
        if self.is_running(run_id):
            return
        task = asyncio.create_task(self._run(run_id, skip_icp), name=f"run-{run_id}")
        self._tasks[run_id] = task
        self._ensure_keepalive()
        if not self._heartbeat or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._beat())

    async def _beat(self) -> None:
        while self.active_count() > 0:
            try:
                await db.run(db.touch_runs, [rid for rid, t in self._tasks.items() if not t.done()])
            except Exception:  # noqa: BLE001 — a missed beat is harmless; 5 missed ones mark the run as orphaned
                log.warning("run heartbeat failed")
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _run(self, run_id: str, skip_icp: bool) -> None:
        try:
            await execute_run(run_id, skip_icp=skip_icp)
        except asyncio.CancelledError:
            if run_id in self._user_cancels:
                await db.run(db.set_status, run_id, "cancelled", "Cancelled by a user; work saved so far is kept")
            else:  # the server is stopping (deploy/restart): paused, can be continued (E-19, D-100)
                await db.run(db.set_status, run_id, "paused",
                             "Stopped when the server restarted; everything found so far is kept",
                             error_message="Interrupted by server restart")
            raise
        finally:
            self._tasks.pop(run_id, None)
            self._user_cancels.discard(run_id)

    def cancel(self, run_id: str) -> bool:
        """A person pressed Cancel (the run ends `cancelled`, unlike a shutdown)."""
        task = self._tasks.get(run_id)
        if task and not task.done():
            self._user_cancels.add(run_id)
            task.cancel()
            return True
        return False

    def _ensure_keepalive(self) -> None:
        url = get_settings().render_external_url
        if not url or (self._keepalive and not self._keepalive.done()):
            return
        self._keepalive = asyncio.create_task(self._keep_awake(url.rstrip("/") + "/health"))

    async def _keep_awake(self, url: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            while self.active_count() > 0:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                try:
                    await client.get(url)
                except httpx.HTTPError:
                    log.warning("keep-alive ping failed")

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:  # let each run write its "interrupted" status before the DB pool closes
            await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
        for extra in (self._keepalive, self._heartbeat):
            if extra:
                extra.cancel()


manager = RunManager()


def recover_orphans() -> int:
    return db.fail_orphaned_runs()
