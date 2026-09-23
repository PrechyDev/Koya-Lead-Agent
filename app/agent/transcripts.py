"""Recover an agent session's real cost from Claude Code's on-disk transcripts.

If a phase is killed, times out or crashes before the SDK reports its cost, the
runner uses this so the spend ledger never under-counts (found in dev run 58876eae).
"""

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from app.lib.budget import cost_from_usage

PROJECTS = Path.home() / ".claude" / "projects"


def session_files(session_id: str) -> list[Path]:
    files: list[Path] = []
    if not PROJECTS.exists():
        return files
    for folder in PROJECTS.iterdir():
        main = folder / f"{session_id}.jsonl"
        if main.exists():
            files.append(main)
            files += sorted((folder / session_id / "subagents").glob("*.jsonl"))
    return files


def usage_by_model(path: Path) -> dict[str, dict[str, int]]:
    totals: dict = defaultdict(lambda: defaultdict(int))
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") or {}
        if entry.get("type") != "assistant" or not msg.get("usage"):
            continue
        if msg.get("id") in seen:  # streamed messages repeat the same id; count once
            continue
        seen.add(msg.get("id"))
        u = msg["usage"]
        m = totals[msg.get("model", "?")]
        m["input"] += u.get("input_tokens", 0)
        m["output"] += u.get("output_tokens", 0)
        m["cache_read"] += u.get("cache_read_input_tokens", 0)
        m["cache_write"] += u.get("cache_creation_input_tokens", 0)
    return totals


def session_cost(session_id: str) -> dict[str, Decimal]:
    per_model: dict[str, Decimal] = defaultdict(Decimal)
    for path in session_files(session_id):
        for model, u in usage_by_model(path).items():
            per_model[model] += cost_from_usage(model, u["input"], u["output"], u["cache_read"], u["cache_write"])
    return dict(per_model)
