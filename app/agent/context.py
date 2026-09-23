"""Per-run state shared by the tools of ONE run (each run builds its own tools)."""

from dataclasses import dataclass, field

from app import db


@dataclass
class RunContext:
    run_id: str
    limits: dict
    cross_run_dedupe: bool = True
    icp: dict | None = None
    finished: bool = False
    scraping_disabled_reason: str | None = None
    status_seen: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, run_id: str) -> "RunContext":
        run = db.get_run(run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        return cls(run_id=str(run["id"]), limits=run["limits"], cross_run_dedupe=run["cross_run_dedupe"],
                   icp=run["icp"])

    def refresh_icp(self) -> dict | None:
        run = db.get_run(self.run_id)
        self.icp = run["icp"] if run else None
        return self.icp

    def hard_filters(self) -> list[str]:
        return list((self.icp or {}).get("hard_filters") or [])

    def move_to(self, status: str, detail: str) -> None:
        """Advance the run's visible status once per phase (set by tools, not by agent narration)."""
        db.set_status(self.run_id, status, detail)
        self.status_seen.add(status)

    def detail(self, detail: str) -> None:
        db.update_run(self.run_id, status_detail=detail)
