"""Turn the spend ledger's raw (source, model) totals into something a person can read (Spend page, D-92).

The ledger records a technical `source` per Claude call and the model name as the API reported it (which can be
"claude-haiku-4-5-20251001" or "claude-opus-5-5[1m]" for the same model). Here each source becomes a named step
of the system, each model a family, with its share of the total and the average cost per call.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

# source -> (what the step is, what it does). Unknown sources are shown as they are.
STEPS: dict[str, tuple[str, str]] = {
    "run": ("Research & drafting", "the agent run: the orchestrator plus the researcher and copywriter subagents"),
    "icp": ("Understanding the objective", "the scope check, then turning the objective into an ICP"),
    "grounding": ("Fact-checking drafts", "checks every claim in the drafts against the scraped pages"),
    "preflight": ("Pre-run key check", "a 1-token call before each run to confirm the AI key works"),
    "eval": ("Model A/B test", "one-off, finished: chose the models for each role"),
    "spike": ("SDK trial", "one-off, day 1: proved the Agent SDK works"),
}
MODEL_NAMES = {"claude-opus": "Opus", "claude-sonnet": "Sonnet", "claude-haiku": "Haiku"}


@dataclass(frozen=True)
class Line:
    name: str
    detail: str
    calls: int
    cost: Decimal
    share: float  # 0..100, of the total spend
    per_call: Decimal
    last_at: object = None


def model_family(model: str | None) -> str:
    """'claude-haiku-4-5-20251001' and 'claude-haiku-4-5' -> 'Haiku 4.5'; 'claude-opus-5-5[1m]' -> 'Opus 5.5'."""
    m = re.match(r"(claude-(?:opus|sonnet|haiku))-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(?:\[\w+\])?$", (model or "").strip())
    if not m:
        return model or "(unknown)"
    version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"{MODEL_NAMES[m.group(1)]} {version}"


def _lines(groups: dict[str, dict], total: Decimal) -> list[Line]:
    out = [Line(name=name, detail=g["detail"], calls=g["calls"], cost=g["cost"],
                share=float(g["cost"] / total * 100) if total else 0.0,
                per_call=(g["cost"] / g["calls"]) if g["calls"] else Decimal(0), last_at=g["last_at"])
           for name, g in groups.items()]
    return sorted(out, key=lambda line: line.cost, reverse=True)


def _add(groups: dict, key: str, detail: str, row: dict) -> None:
    g = groups.setdefault(key, {"detail": detail, "calls": 0, "cost": Decimal(0), "last_at": None})
    g["calls"] += int(row["entries"])
    g["cost"] += Decimal(str(row["cost_usd"] or 0))
    if row.get("last_at") and (g["last_at"] is None or row["last_at"] > g["last_at"]):
        g["last_at"] = row["last_at"]


def breakdown(rows: list[dict]) -> tuple[list[Line], list[Line]]:
    """(by step, by model) from spend_summary_v rows (source, model, entries, cost_usd, last_at)."""
    total = sum((Decimal(str(r["cost_usd"] or 0)) for r in rows), Decimal(0))
    steps: dict[str, dict] = {}
    models: dict[str, dict] = {}
    used_for: dict[str, set[str]] = {}
    for r in rows:
        name, detail = STEPS.get(r["source"], (r["source"], ""))
        _add(steps, name, detail, r)
        family = model_family(r["model"])
        _add(models, family, "", r)
        used_for.setdefault(family, set()).add(name)
    for family, names in used_for.items():
        models[family]["detail"] = "Used for: " + ", ".join(sorted(names))
    return _lines(steps, total), _lines(models, total)
