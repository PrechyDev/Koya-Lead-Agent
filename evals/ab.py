"""One-off model A/B test to choose the cheapest accurate model per step (specs.md §9). Dev-only tool.

Record once, replay many: fixtures come from real runs already paid for; replays call Claude directly via
the Message Batches API (50% cheaper) with the SAME skill text the production agent uses. No Apify or
Firecrawl spend. Every replay is scored by code and written to lead_agent.eval_results + the spend ledger.

    python evals/ab.py export  <run_id> [<run_id> ...] --name prd_example      # free: build fixtures
    python evals/ab.py estimate --name prd_example                             # free: pre-flight cost
    python evals/ab.py reference --name prd_example --yes                      # Opus 5.5 answer key (~$0.15)
    python evals/ab.py run --name prd_example --yes                            # Haiku vs Sonnet vs Opus (estimate first)
    python evals/ab.py report --name prd_example                               # markdown table for progress.md

The run refuses to start if the estimate would push eval spend past EVAL_CAP_USD ($1.20).
"""

import argparse
import json
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

log = logging.getLogger("lead_agent.evals.ab")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.agent import prompts  # noqa: E402
from app.agent.tools import ICPModel  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.lib.budget import BudgetExceeded, assert_can_spend, cost_from_usage  # noqa: E402
from app.lib.outreach_checks import check_outreach  # noqa: E402
from app.lib.qualification_rules import DisqualifierCheck, HardFilterCheck, SoftPreferenceCheck  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
SKILLS = ROOT / "agent_plugin" / "skills"
EVAL_CAP_USD = Decimal("1.20")  # owner-approved 2026-09-24: +$0.30 so Opus 5.5 can compete on two stages (D-59)
CANDIDATES = ["claude-haiku-4-5", "claude-sonnet-5"]
REFERENCE_MODEL = "claude-opus-5-5"
# Opus 5.5 also competes where quality matters most and a quality gain could justify ~2x the price. On
# qualification it is scored against its own reference labels, so its agreement is self-consistency, not
# accuracy: the owner spot-checks its disagreements with Sonnet before choosing it.
OPUS_STAGES = {"qual", "copy"}
REPEATS = 2

ICP_CASES = [
    {"id": "vague", "objective": "Find companies that might need AI automation help",
     "searchable": True, "must_preserve": []},
    {"id": "specific", "objective": "Find 10 US-based B2B SaaS companies with 20 to 80 employees selling to healthcare "
                                    "providers; exclude agencies.",
     "searchable": True, "must_preserve": ["us", "20", "80", "healthcare", "agenc"]},
    {"id": "unsearchable", "objective": "find leads", "searchable": False, "must_preserve": []},
]

# Drafts with planted fake facts (the checker should catch these) and clean ones (it should pass).
PLANTED = [
    ("raised a $40M Series B last month", True), ("was named the #1 CPQ tool by Gartner", True),
    ("has 3,000 enterprise customers in Europe", True), (None, False), (None, False), (None, False),
]


# ---------------------------------------------------------------------------
# Structured-output models (mirror the production tool inputs)
# ---------------------------------------------------------------------------
class ICPOut(BaseModel):
    icp: ICPModel
    is_searchable: bool
    clarification_question: str | None = None


class QualOut(BaseModel):
    status: Literal["qualified", "not_qualified", "needs_review"]
    hard_filter_checks: list[HardFilterCheck]
    disqualifier_checks: list[DisqualifierCheck]
    soft_preference_checks: list[SoftPreferenceCheck]
    fit_reasons: list[str]
    concerns: list[str]
    source_summary: str


class EmailOut(BaseModel):
    step: int
    subject: str
    body: str
    personalization_note: str
    evidence_ref: str


class CopyOut(BaseModel):
    emails: list[EmailOut]
    linkedin_message: str


class ClaimOut(BaseModel):
    text: str
    about: Literal["company", "koya", "other"]
    supported: bool
    evidence: str


class GroundOut(BaseModel):
    claims: list[ClaimOut]
    unsupported_count: int
    summary: str


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema -> the strict shape structured outputs require (no extra keys, all required)."""
    schema = model.model_json_schema()

    def fix(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "default", "title"):
                node.pop(key, None)
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
    fix(schema)
    return schema


def skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def export(run_ids: list[str], name: str) -> None:
    companies = []
    icp = None
    for run_id in run_ids:
        run = db.get_run(run_id)
        icp = icp or run["icp"]
        for lead in db.list_leads(run_id):
            if lead["prescreen_result"] and lead["prescreen_result"].startswith("rejected"):
                continue
            pages = []
            for url in lead["fetched_urls"] or []:
                page = db.get_cached_page(url, 60)
                if page and page["content"]:
                    pages.append({"url": url, "content": page["content"]})
            if not pages:
                continue
            companies.append({"domain": lead["company_domain"], "name": lead["company_name"],
                              "linkedin_url": lead["linkedin_url"], "discovery": lead["discovery_data"],
                              "pages": pages, "source_run": run_id})
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{name}.json"
    dedup = {c["domain"]: c for c in companies}
    out.write_text(json.dumps(db.to_jsonable({"icp": icp, "companies": list(dedup.values())}), indent=1),
                   encoding="utf-8")
    log.info(f"wrote {out} with {len(dedup)} companies")


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_reference(name: str) -> dict:
    path = FIXTURES / f"{name}.reference.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------------------------------------------------------------------------
# Request builders (one per stage). Each returns (custom_id, params, scorer_context).
# ---------------------------------------------------------------------------
def _params(model: str, system: str, user: str, out_model: type[BaseModel], max_tokens: int = 3000,
            effort: str | None = None) -> dict:
    output_config: dict = {"format": {"type": "json_schema", "schema": strict_schema(out_model)}}
    if effort and not model.startswith("claude-haiku"):
        output_config["effort"] = effort
    return {"model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}], "output_config": output_config}


def _company_block(c: dict) -> str:
    d = c["discovery"] or {}
    facts = {k: d.get(k) for k in ("tagline", "description", "industries", "employee_count_range",
                                   "employee_count_linkedin", "hq", "founded_year")}
    pages = "\n\n".join(f'<untrusted_website_content url="{p["url"]}">\n{p["content"][:6000]}\n</untrusted_website_content>'
                        for p in c["pages"][:2])
    return (f"Company: {c['name']} ({c['domain']})\nLinkedIn: {c['linkedin_url']}\n"
            f"Discovery facts: {json.dumps(facts)}\n\n{pages}")


def qual_request(model: str, c: dict, icp: dict, effort: str | None = None) -> dict:
    system = prompts.RESEARCHER_PROMPT + "\n\n" + skill("lead-qualification")
    user = (f"ICP hard filters (use the text exactly): {json.dumps(icp.get('hard_filters'))}\n"
            f"Disqualifiers: {json.dumps(icp.get('disqualifiers'))}\n"
            f"Soft preferences: {json.dumps(icp.get('soft_preferences'))}\n\n{_company_block(c)}\n\n"
            "Return your qualification decision. Allowed source URLs: the LinkedIn URL and the page URLs above.")
    return _params(model, system, user, QualOut, effort=effort)


def icp_request(model: str, case: dict) -> dict:
    return _params(model, prompts.ICP_SYSTEM + "\n\n" + skill("icp-refinement"),
                   prompts.icp_user_prompt(case["objective"]), ICPOut)


def copy_request(model: str, c: dict, reference: dict) -> dict:
    system = prompts.COPYWRITER_PROMPT + "\n\n" + skill("outbound-copywriting")
    ref = reference.get(c["domain"], {})
    facts = {"company": c["name"], "domain": c["domain"], "source_summary": ref.get("source_summary"),
             "fit_reasons": ref.get("fit_reasons"), "evidence": ref.get("hard_filter_checks"),
             "source_urls": [c["linkedin_url"]] + [p["url"] for p in c["pages"]]}
    return _params(model, system, f"Lead facts (use only these):\n{json.dumps(facts)}", CopyOut)


def ground_request(model: str, c: dict, draft: dict) -> dict:
    from app.services.grounding import SYSTEM, build_prompt
    context = _company_block(c)
    return _params(model, SYSTEM, build_prompt(context, draft["emails"], draft["linkedin_message"]), GroundOut)


def planted_draft(c: dict, fake: str | None) -> dict:
    line = f"I noticed {c['name']} {fake}." if fake else f"I noticed {c['name']} works with growing teams."
    body = f"Hi {{{{first_name}}}}, {line} Is there repetitive ops work an automation assistant could take on?"
    return {"emails": [{"step": i, "subject": "Quick question", "body": body, "personalization_note": "n",
                        "evidence_ref": c["pages"][0]["url"]} for i in (1, 2, 3)],
            "linkedin_message": f"Hi {{{{first_name}}}}, {line}"}


# ---------------------------------------------------------------------------
# Batch plumbing, estimation, budget
# ---------------------------------------------------------------------------
def estimate_cost(requests: list[tuple[str, dict]]) -> Decimal:
    total = Decimal("0")
    for _, p in requests:
        chars = len(json.dumps(p["system"])) + len(json.dumps(p["messages"]))
        total += cost_from_usage(p["model"], chars // 4, 900 if "opus" in p["model"] else 700, batch=True)
    return total.quantize(Decimal("0.0001"))


def eval_spent() -> Decimal:
    row = db.fetch_one(f"select coalesce(sum(cost_usd), 0) as s from {db.t('spend_ledger')} where source = 'eval'")
    return Decimal(str(row["s"]))


def run_batch(requests: list[tuple[str, dict]], label: str) -> dict[str, dict]:
    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    batch = client.messages.batches.create(requests=[{"custom_id": cid, "params": p} for cid, p in requests])
    log.info(f"batch {batch.id} submitted ({len(requests)} requests); waiting…")
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(20)
    results: dict[str, dict] = {}
    for item in client.messages.batches.results(batch.id):
        if item.result.type != "succeeded":
            results[item.custom_id] = {"error": item.result.type}
            continue
        msg = item.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "{}")
        u = msg.usage
        cost = cost_from_usage(msg.model, u.input_tokens, u.output_tokens,
                               getattr(u, "cache_read_input_tokens", 0) or 0,
                               getattr(u, "cache_creation_input_tokens", 0) or 0, batch=True)
        db.record_spend("eval", cost, model=msg.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        note=f"{label} {item.custom_id}")
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"error": "invalid json"}
        results[item.custom_id] = {"output": parsed, "cost": float(cost), "stop": msg.stop_reason}
    return results


def guard(requests: list[tuple[str, dict]], yes: bool) -> None:
    est = estimate_cost(requests)
    spent = eval_spent()
    log.info(f"pre-flight: {len(requests)} requests, estimated ${est} (batch price); eval spent so far ${spent:.4f}; "
          f"cap ${EVAL_CAP_USD}")
    if spent + est > EVAL_CAP_USD:
        raise SystemExit("REFUSED: this would exceed the eval cap. Shrink the test set or raise the cap on purpose.")
    try:  # and the project's hard Claude budget, like every other paid call (specs §7.2, E-29)
        assert_can_spend(db.total_spend(), est, get_settings().claude_budget_total_usd)
    except BudgetExceeded as exc:
        raise SystemExit(f"REFUSED: {exc}") from None
    if not yes:
        raise SystemExit("Dry run only. Re-run with --yes to spend.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def build_reference_requests(name: str) -> list[tuple[str, dict]]:
    fx = load(name)
    return [(f"ref::{c['domain']}", qual_request(REFERENCE_MODEL, c, fx["icp"], effort="medium")) for c in fx["companies"]]


def build_ab_requests(name: str) -> list[tuple[str, dict]]:
    fx, ref = load(name), load_reference(name)
    reqs: list[tuple[str, dict]] = []
    qualified = [c for c in fx["companies"] if ref.get(c["domain"], {}).get("status") == "qualified"][:3]
    for model in CANDIDATES + [REFERENCE_MODEL]:
        opus = model == REFERENCE_MODEL
        for rep in range(1, REPEATS + 1):
            for case in ([] if opus else ICP_CASES):
                reqs.append((f"icp::{model}::{rep}::{case['id']}", icp_request(model, case)))
            for c in fx["companies"][:8]:
                reqs.append((f"qual::{model}::{rep}::{c['domain']}", qual_request(model, c, fx["icp"])))
            for c in qualified:
                reqs.append((f"copy::{model}::{rep}::{c['domain']}", copy_request(model, c, ref)))
            for i, (fake, _) in enumerate([] if opus else PLANTED):
                c = fx["companies"][i % len(fx["companies"])]
                reqs.append((f"ground::{model}::{rep}::{i}", ground_request(model, c, planted_draft(c, fake))))
    return reqs


def score(name: str, results: dict[str, dict]) -> None:
    fx, ref = load(name), load_reference(name)
    by_domain = {c["domain"]: c for c in fx["companies"]}
    for cid, res in results.items():
        stage, model, rep, case = cid.split("::")
        out = res.get("output") or {}
        passed, score_value, notes, expected, actual = None, None, "", None, None
        if "error" in res or "error" in out:
            passed, notes = False, str(res.get("error") or out.get("error"))
        elif stage == "icp":
            c = next(x for x in ICP_CASES if x["id"] == case)
            text = json.dumps(out).lower()
            preserved = all(k in text for k in c["must_preserve"])
            passed = (out.get("is_searchable") == c["searchable"]) and preserved
            expected, actual = {"searchable": c["searchable"], "preserve": c["must_preserve"]}, out.get("is_searchable")
        elif stage == "qual":
            exp = ref.get(case, {}).get("status")
            got = out.get("status")
            passed = exp is not None and got == exp
            false_qualified = got == "qualified" and exp != "qualified"
            score_value = 1 if passed else 0
            expected, actual = exp, got
            notes = "FALSE QUALIFIED" if false_qualified else ""
        elif stage == "copy":
            c = by_domain[case]
            sources = [c["linkedin_url"]] + [p["url"] for p in c["pages"]]
            problems = check_outreach([e for e in out.get("emails", [])], out.get("linkedin_message", ""), sources)
            passed, score_value, notes = not problems, len(problems), "; ".join(problems)[:500]
        elif stage == "ground":
            fake, should_flag = PLANTED[int(case)]
            flagged = any(not cl.get("supported") and cl.get("about") in ("company", "koya")
                          for cl in out.get("claims", []))
            passed = flagged == should_flag
            expected, actual = should_flag, flagged
            notes = f"planted: {fake}" if fake else "clean draft"
        db.insert_eval_result(eval_name=name, stage=stage, model=model, repeat_no=int(rep), case_id=case,
                              expected=expected, actual=actual if not isinstance(actual, str) else {"value": actual},
                              passed=passed, score=score_value, cost_usd=res.get("cost"), notes=notes)


def report(name: str) -> None:
    rows = db.fetch_all(
        f"""select stage, model, repeat_no, count(*) as n, sum(case when passed then 1 else 0 end) as ok,
                   sum(case when notes = 'FALSE QUALIFIED' then 1 else 0 end) as false_q, sum(cost_usd) as cost
            from {db.t('eval_results')} where eval_name = %s group by 1, 2, 3 order by 1, 2, 3""", (name,))
    log.info("| Step | Model | Repeat | Passed | False qualified | Cost $ |")
    log.info("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        log.info(f"| {r['stage']} | {r['model']} | {r['repeat_no']} | {r['ok']}/{r['n']} | {r['false_q']} | "
              f"{float(r['cost'] or 0):.4f} |")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["export", "estimate", "reference", "run", "report"])
    parser.add_argument("run_ids", nargs="*")
    parser.add_argument("--name", default="prd_example")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.command == "export":
        export(args.run_ids, args.name)
    elif args.command == "estimate":
        ref = build_reference_requests(args.name)
        log.info(f"reference (Opus 5.5, {len(ref)} companies): est ${estimate_cost(ref)}")
        if load_reference(args.name):
            ab = build_ab_requests(args.name)
            log.info(f"A/B replays ({len(ab)} requests): est ${estimate_cost(ab)}")
        else:
            log.info("A/B estimate needs the reference labels first (run `reference`).")
        log.info(f"eval spent so far: ${eval_spent():.4f} of ${EVAL_CAP_USD}")
    elif args.command == "reference":
        reqs = build_reference_requests(args.name)
        guard(reqs, args.yes)
        results = run_batch(reqs, "reference")
        labels = {cid.split("::", 1)[1]: r.get("output", {}) for cid, r in results.items()}
        (FIXTURES / f"{args.name}.reference.json").write_text(json.dumps(labels, indent=1), encoding="utf-8")
        log.info(f"reference labels saved for {len(labels)} companies")
    elif args.command == "run":
        reqs = build_ab_requests(args.name)
        guard(reqs, args.yes)
        results = run_batch(reqs, "ab")
        score(args.name, results)
        report(args.name)
    elif args.command == "report":
        report(args.name)
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
