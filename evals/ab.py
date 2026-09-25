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
from app.lib.icp_defaults import has_company_type  # noqa: E402
from app.lib.outreach_checks import check_outreach  # noqa: E402
from app.lib.qualification_rules import DisqualifierCheck, HardFilterCheck, SoftPreferenceCheck  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
SKILLS = ROOT / "agent_plugin" / "skills"
EVAL_CAP_USD = Decimal("1.20")  # owner-approved 2026-09-24: +$0.30 so Opus 5.5 can compete on two stages (D-59)
CANDIDATES = ["claude-haiku-4-5", "claude-sonnet-5"]
REFERENCE_MODEL = "claude-opus-5-5"
# Opus 5.5 also competes on copywriting, where quality matters most and a gain could justify ~2x the price.
# Not on qualification (owner, 2026-09-25): its answer key IS Opus's qualification, so a replay would only
# measure self-consistency (~$0.35). Its qualification quality is read from the reference itself.
OPUS_STAGES = {"copy"}
COPY_SET_SIZE = 3  # qualified companies first, then the needs-review ones with the most passed hard filters
REPEATS = 2
QUAL_MAX_TOKENS = 8000

ICP_CASES = [
    # No kind of company named: since D-58 the right answer is to ASK (save_icp enforces it). This case expected
    # "searchable" before D-58, which marked the correct answer wrong (errors log #66).
    {"id": "vague", "objective": "Find companies that might need AI automation help",
     "searchable": False, "must_preserve": []},
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
        if run is None:
            raise SystemExit(f"Run {run_id} not found.")
        icp = icp or run["icp"]
        for lead in db.list_leads(run_id):
            if lead["prescreen_result"] and lead["prescreen_result"].startswith("rejected"):
                continue
            pages = []
            for url in lead["fetched_urls"] or []:
                page = db.get_cached_page(url, get_settings().scrape_cache_days)
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
    # 8000, not 3000: Sonnet's thinking counts as output and two answers were cut off at 3000 (errors log #72);
    # the production agent has no such limit, so a cut-off answer measured the rig, not the model.
    return _params(model, system, user, QualOut, max_tokens=QUAL_MAX_TOKENS, effort=effort)


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


def page_sentence(c: dict) -> str:
    """A real sentence from the company's own page (6-30 words), so a clean control draft is truly supported."""
    import re
    text = " ".join(p["content"] for p in c["pages"])
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = sentence.split()
        clean = not re.search(r"[\[\]|*#\n]|cookie|privacy", sentence, re.I)  # skip banners, markup, broken lines
        if 6 <= len(words) <= 30 and sentence.endswith(".") and clean:
            return sentence.strip()
    return f"{c['name']} is a company with a public website."


def planted_draft(c: dict, fake: str | None) -> dict:
    # Clean controls quote the page (errors log #70: "works with growing teams" was itself unsupported, so a
    # strict checker was scored as raising a false alarm).
    line = f"I noticed {c['name']} {fake}." if fake else f"I read on your site: \"{page_sentence(c)}\""
    body = f"Hi {{{{first_name}}}}, {line} Is there repetitive ops work an automation assistant could take on?"
    return {"emails": [{"step": i, "subject": "Quick question", "body": body, "personalization_note": "n",
                        "evidence_ref": c["pages"][0]["url"]} for i in (1, 2, 3)],
            "linkedin_message": f"Hi {{{{first_name}}}}, {line}"}


# ---------------------------------------------------------------------------
# Batch plumbing, estimation, budget
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_TOKENS = {"opus": 900, "other": 700}  # a guess, used only until real answers exist


def observed_output_tokens() -> dict[str, float]:
    """Average output tokens per model from answers already paid for (thinking is billed as output)."""
    rows = db.fetch_all(f"select model, avg(output_tokens) as avg_out from {db.t('spend_ledger')} "
                        "where source = 'eval' and output_tokens is not null group by model")
    return {r["model"]: float(r["avg_out"]) for r in rows}


def _output_guess(model: str, observed: dict[str, float]) -> int:
    for known, avg in observed.items():
        if known.startswith(model) or model.startswith(known):
            return int(avg * 1.25) + 1  # 25% headroom over what this model actually wrote
    return DEFAULT_OUTPUT_TOKENS["opus" if "opus" in model else "other"]


def estimate_cost(requests: list[tuple[str, dict]]) -> Decimal:
    observed = observed_output_tokens()
    total = Decimal("0")
    for _, p in requests:
        chars = len(json.dumps(p["system"])) + len(json.dumps(p["messages"]))
        out = min(p["max_tokens"], _output_guess(p["model"], observed))
        total += cost_from_usage(p["model"], chars // 4, out, batch=True)
    return total.quantize(Decimal("0.0001"))


def worst_case_cost(requests: list[tuple[str, dict]]) -> Decimal:
    """If every answer used its full max_tokens: the most this batch can cost."""
    total = sum((cost_from_usage(p["model"], (len(json.dumps(p["system"])) + len(json.dumps(p["messages"]))) // 4,
                                 p["max_tokens"], batch=True) for _, p in requests), Decimal("0"))
    return total.quantize(Decimal("0.0001"))


def eval_spent() -> Decimal:
    row = db.fetch_one(f"select coalesce(sum(cost_usd), 0) as s from {db.t('spend_ledger')} where source = 'eval'")
    return Decimal(str(row["s"]))


def run_batch(requests: list[tuple[str, dict]], label: str) -> dict[str, dict]:
    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    # Batch ids may only use [a-zA-Z0-9_-] (max 64); ours contain "::" and domains, so send short ids and map back.
    names = {f"r{i:04d}": cid for i, (cid, _) in enumerate(requests)}
    safe = {cid: sid for sid, cid in names.items()}
    batch = client.messages.batches.create(requests=[{"custom_id": safe[cid], "params": p} for cid, p in requests])
    # Saved at once: if this script dies while waiting, `collect` fetches the (already paid) answers later.
    pending = FIXTURES / f"batch_{label}_{batch.id}.pending.json"
    pending.write_text(json.dumps({"batch_id": batch.id, "label": label, "names": names}), encoding="utf-8")
    log.info(f"batch {batch.id} submitted ({len(requests)} requests); waiting…")
    return collect(batch.id)


def collect(batch_id: str) -> dict[str, dict]:
    """Wait for a submitted batch (tolerating network drops), record its spend once, save and return the answers."""
    pending = next(FIXTURES.glob(f"batch_*_{batch_id}.pending.json"), None)
    if pending is None:
        raise SystemExit(f"No pending file for {batch_id}: its id map is unknown.")
    info = json.loads(pending.read_text(encoding="utf-8"))
    names, label = info["names"], info["label"]
    saved = FIXTURES / f"batch_{label}_{batch_id}.results.json"
    if saved.exists():  # already collected: never record its spend twice
        log.info(f"{batch_id} was already collected ({saved.name})")
        return json.loads(saved.read_text(encoding="utf-8"))
    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    while True:
        try:
            batch = client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                break
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            log.info(f"waiting for {batch_id}: {type(exc).__name__}, retrying")  # the batch keeps running remotely
        time.sleep(20)
    results: dict[str, dict] = {}
    for item in client.messages.batches.results(batch_id):
        item_id = names[item.custom_id]
        if item.result.type in ("canceled", "expired"):
            continue  # never ran (and isn't billed): not a wrong answer, so it isn't scored (errors log #71)
        if item.result.type != "succeeded":
            results[item_id] = {"error": item.result.type}
            continue
        msg = item.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "{}")
        u = msg.usage
        cost = cost_from_usage(msg.model, u.input_tokens, u.output_tokens,
                               getattr(u, "cache_read_input_tokens", 0) or 0,
                               getattr(u, "cache_creation_input_tokens", 0) or 0, batch=True)
        db.record_spend("eval", cost, model=msg.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        note=f"{label} {item_id}")
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"error": "invalid json"}
        results[item_id] = {"output": parsed, "cost": float(cost), "stop": msg.stop_reason,
                            "output_tokens": u.output_tokens}
    if not results:
        log.info(f"{batch_id} returned no answers (cancelled or expired); nothing to score")
        return results
    saved.write_text(json.dumps(results, indent=1), encoding="utf-8")  # kept: answers already paid for
    log.info(f"batch results saved to {saved}")
    return results


def guard(requests: list[tuple[str, dict]], yes: bool) -> None:
    est = estimate_cost(requests)
    spent = eval_spent()
    log.info(f"pre-flight: {len(requests)} requests, estimated ${est} (worst case ${worst_case_cost(requests)}), "
             f"batch price; eval spent so far ${spent:.4f}; cap ${EVAL_CAP_USD}")
    if spent + est > EVAL_CAP_USD:
        raise SystemExit("REFUSED: this would exceed the eval cap. Shrink the test set or raise the cap on purpose.")
    try:  # and the project's hard Claude budget: refused if even the WORST case could pass it (rule 5, E-29)
        assert_can_spend(db.total_spend(), worst_case_cost(requests), get_settings().claude_budget_total_usd)
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


def build_ab_requests(name: str, repeats: int = REPEATS) -> list[tuple[str, dict]]:
    fx, ref = load(name), load_reference(name)
    reqs: list[tuple[str, dict]] = []
    # Owner, 2026-09-25: the answer key had 1 qualified company, so the writing test also uses needs-review
    # companies with the most evidence. Still a fair test: every fact must come from their pages.
    def passes(c: dict) -> int:
        return sum(ch.get("result") == "pass" for ch in ref.get(c["domain"], {}).get("hard_filter_checks", []))
    rank = {"qualified": 0, "needs_review": 1}
    pool = [c for c in fx["companies"] if ref.get(c["domain"], {}).get("status") in rank]
    qualified = sorted(pool, key=lambda c: (rank[ref[c["domain"]]["status"]], -passes(c)))[:COPY_SET_SIZE]
    for model in CANDIDATES + [REFERENCE_MODEL]:
        def runs(stage: str, cases: list, m: str = model) -> list:
            return cases if m != REFERENCE_MODEL or stage in OPUS_STAGES else []  # Opus only in OPUS_STAGES
        for rep in range(1, repeats + 1):
            for case in runs("icp", ICP_CASES):
                reqs.append((f"icp::{model}::{rep}::{case['id']}", icp_request(model, case)))
            for c in runs("qual", fx["companies"][:8]):
                reqs.append((f"qual::{model}::{rep}::{c['domain']}", qual_request(model, c, fx["icp"])))
            for c in runs("copy", qualified):
                reqs.append((f"copy::{model}::{rep}::{c['domain']}", copy_request(model, c, ref)))
            for i, (fake, _) in enumerate(runs("ground", PLANTED)):
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
            # Score what the server would STORE: save_icp makes an ICP with no company type a question (D-58).
            stored_searchable = bool(out.get("is_searchable")) and has_company_type(out.get("icp") or {})
            passed = (stored_searchable == c["searchable"]) and preserved
            expected, actual = {"searchable": c["searchable"], "preserve": c["must_preserve"]}, out.get("is_searchable")
        elif stage == "qual":
            exp = ref.get(case, {}).get("status")
            got = out.get("status")
            passed = exp is not None and got == exp
            false_qualified = got == "qualified" and exp is not None and exp != "qualified"  # needs a label
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


def fact_check_copy(name: str) -> None:
    """Run the app's own fact-checker (MODEL_GROUNDING, as in production) over every saved copywriting answer,
    against the company's pages + the answer-key facts, and print a table. Spec §9: copy is judged on the writing
    rules AND on unsupported claims. ~$0.002 per draft set; each call is recorded in the spend ledger."""
    import asyncio

    from app.services.grounding import check_grounding
    fx, ref = load(name), load_reference(name)
    by_domain = {c["domain"]: c for c in fx["companies"]}
    answers: dict[str, dict] = {}
    for f in sorted(FIXTURES.glob("batch_ab-copy_*.results.json")):
        answers.update(json.loads(f.read_text(encoding="utf-8")))
    rows = []
    for cid, res in sorted(answers.items()):
        _, model, rep, domain = cid.split("::")
        out = res.get("output") or {}
        c = by_domain[domain]
        sources = [c["linkedin_url"]] + [p["url"] for p in c["pages"]]
        emails = out.get("emails") or []
        rules = check_outreach(emails, out.get("linkedin_message", ""), sources) if emails else ["no answer"]
        facts = ref.get(domain, {})
        context = (f"Company: {c['name']} ({domain})\nSource summary: {facts.get('source_summary')}\n"
                   f"Fit reasons: {facts.get('fit_reasons')}\n" + _company_block(c))
        g = asyncio.run(check_grounding(context, emails, out.get("linkedin_message", ""))) if emails else None
        if g and g.cost_usd:
            db.record_spend("eval", g.cost_usd, model=g.model, input_tokens=g.input_tokens,
                            output_tokens=g.output_tokens, note=f"copy fact-check {cid}")
        rows.append((model, domain, len(rules), len(g.unsupported) if g else None,
                     len(g.missing_specifics) if g else None, g.error if g else "no answer", rules, g))
    log.info("| Model | Company | Rule problems | Unsupported claims | Emails without a real company fact | Checker note |")
    log.info("| --- | --- | --- | --- | --- | --- |")
    for model, domain, nr, nu, nm, err, _rules, _g in rows:
        log.info(f"| {model} | {domain} | {nr} | {nu} | {nm} | {err or ''} |")
    for model, domain, _nr, _nu, _nm, _err, rules, g in rows:
        for r in rules:
            log.info(f"  rule  {model} {domain}: {r}")
        for u in (g.unsupported if g else []):
            log.info(f"  claim {model} {domain}: {u}")
    (FIXTURES / f"{name}.copy_factcheck.json").write_text(json.dumps(
        [{"model": m, "domain": d, "rule_problems": r, "unsupported": g.unsupported if g else None,
          "missing_specifics": g.missing_specifics if g else None} for m, d, _a, _b, _c, _e, r, g in rows],
        indent=1), encoding="utf-8")


def rescore(name: str, stages: set[str]) -> None:
    """Score saved answers again (free: no API calls), e.g. after fixing a scorer. The app's DB role can't
    DELETE (least privilege), so the old rows are removed with the local admin DSN (laptop only)."""
    import psycopg
    from dotenv import dotenv_values
    results: dict[str, dict] = {}
    for f in sorted(FIXTURES.glob("batch_ab-*.results.json")):
        results.update({k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
                        if k.split("::", 1)[0] in stages})
    admin = (dotenv_values(ROOT / ".env").get("SUPABASE_ADMIN_DSN") or "").strip()
    if not admin:
        raise SystemExit("SUPABASE_ADMIN_DSN is needed (locally) to replace old eval rows.")
    with psycopg.connect(admin, prepare_threshold=None, autocommit=True) as conn:
        n = conn.execute(f"delete from {db.t('eval_results')} where eval_name = %s and stage = any(%s)",
                         (name, sorted(stages))).rowcount
    log.info(f"removed {n} old rows; re-scoring {len(results)} saved answers")
    score(name, results)
    report(name)


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
    parser.add_argument("command", choices=["export", "estimate", "reference", "run", "collect", "rescore",
                                            "fact-check-copy", "report"])
    parser.add_argument("--batch-id", help="for `collect`: a batch submitted earlier whose answers weren't fetched")
    parser.add_argument("--repeats", type=int, default=REPEATS, help="for `run`/`estimate`: repeats per case")
    parser.add_argument("--ids", help="for `run`: only these request ids (comma list), e.g. to redo cut-off answers")
    parser.add_argument("run_ids", nargs="*")
    parser.add_argument("--name", default="prd_example")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--stages", default="icp,qual,copy,ground",
                        help="comma list for `run`/`estimate`: run stage by stage so spend is re-checked in between")
    args = parser.parse_args()
    stages = {x.strip() for x in args.stages.split(",") if x.strip()}
    only_ids = {x.strip() for x in (args.ids or "").split(",") if x.strip()}

    if args.command == "export":
        export(args.run_ids, args.name)
    elif args.command == "estimate":
        ref = build_reference_requests(args.name)
        log.info(f"reference (Opus 5.5, {len(ref)} companies): est ${estimate_cost(ref)}")
        if load_reference(args.name):
            for st in sorted(stages):
                part = [r for r in build_ab_requests(args.name, args.repeats) if r[0].startswith(st + "::")]
                log.info(f"A/B {st:<6} ({len(part):>3} requests): est ${estimate_cost(part)}, "
                         f"worst ${worst_case_cost(part)}")
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
        reqs = [r for r in build_ab_requests(args.name, args.repeats) if r[0].split("::", 1)[0] in stages
                and (not only_ids or r[0] in only_ids)]
        guard(reqs, args.yes)
        results = run_batch(reqs, "ab-" + "-".join(sorted(stages)))
        score(args.name, results)
        report(args.name)
    elif args.command == "collect":
        results = collect(args.batch_id)
        if next(iter(results), "").startswith("ref::"):
            labels = {cid.split("::", 1)[1]: r.get("output", {}) for cid, r in results.items()}
            (FIXTURES / f"{args.name}.reference.json").write_text(json.dumps(labels, indent=1), encoding="utf-8")
        else:
            score(args.name, results)
            report(args.name)
    elif args.command == "fact-check-copy":
        fact_check_copy(args.name)
    elif args.command == "rescore":
        rescore(args.name, stages)
    elif args.command == "report":
        report(args.name)
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
