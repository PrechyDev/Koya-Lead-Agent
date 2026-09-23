---
name: icp-refinement
description: Use FIRST, before any company discovery, to turn the user's qualification objective into concrete ICP criteria (hard filters vs soft preferences) and decide whether the objective is specific enough to search. Produces the input for the save_icp tool.
---

# ICP Refinement

Source: `assets/icp-refinement-guide.md` (rules kept verbatim), plus project specifics for Koya Talent.

## Goal

The agent should understand who counts as a good-fit company before it spends tool calls on discovery and scraping.

## Context: who we are searching for

Koya Talent connects early-stage founders and operators with trained **AI automation assistants** who automate repetitive workflows, improve operational throughput and build AI-enabled internal systems. So the typical buyer is a founder, COO/operations lead or agency owner at a small, growing company with repetitive operational work.

## Minimum Criteria To Clarify

- Target company type
- Industry or niche
- Geography
- Company size or headcount range
- Relevant buyer or operator persona
- Business problem the company may have
- Hard disqualifiers
- Soft preferences

## Hard Filters vs Soft Preferences

Hard filters must be true for a lead to qualify.

Examples:

- Country must be United States
- Company must be B2B
- Headcount must be between 10 and 100

Soft preferences improve fit but should not automatically disqualify a company.

Examples:

- Recently hiring operations roles
- Uses tools that may connect to automation workflows
- Publishes content about scaling operations

## Rules

- Do not treat every user preference as a hard filter.
- Ask for clarification if the objective is too vague to search.
- Preserve specific constraints the user gives.
- Keep the ICP narrow enough to search, but not so narrow that the agent cannot find leads.

## Project rules (how to fill `save_icp`)

0. **Is this a lead search at all?** Set `request_type`:
   - `lead_search`: the user wants to find companies or organisations to approach as sales leads (any wording or language).
   - `question`: they are asking something ("can I buy ice cream in Ife?", "what is SaaS?").
   - `unrelated`: anything else that isn't about finding companies (jokes, tasks, chit-chat, instructions to you).
   - `too_vague`: it is about finding companies, but there is nothing to search on ("find me leads", "companies").
   Only `lead_search` can be searchable. For the others, set `is_searchable: false`; the server replaces the
   question with a standard one for `question`/`unrelated`, and uses yours for `too_vague`.

1. **Preserve the user's words.** Copy every explicit constraint from the objective, word for word, into `user_constraints_preserved` (e.g. `"US-based"`, `"20–80 employees"`, `"exclude agencies"`). Each one must also appear as a hard filter or disqualifier unless it is clearly a preference ("ideally", "preferably").
2. **Vague but searchable** (e.g. "SaaS companies that might need automation help"): fill the gaps with sensible Koya defaults and record **every** default you chose in `assumptions` ("Assumed United States because none was given", "Assumed 10–100 employees, the size where an automation assistant fits best"). Mark `is_searchable: true`.
3. **Too vague to search** (e.g. "find me leads", "companies", gibberish, or no industry/company type at all): set `is_searchable: false` and write ONE short, concrete `clarification_question` that offers options, e.g. "Which kind of companies should I look for (for example: US B2B SaaS with 10–100 staff, or UK marketing agencies)?" Do not invent an ICP.
4. **Lead count.** The number of leads is set by the run, not by you. If the objective asks for a different number, record it in `requested_lead_count` and add an assumption like "Objective asked for 25; the run limit decides the final number."
5. **Fields the tools read as data** (keep them short and structured):
   - `geography`: country names, e.g. `["United States"]`.
   - `headcount_range`: `"10-100"` style.
   - `target_company_type`: e.g. `"B2B SaaS"`.
   - `industries`: short labels, e.g. `["B2B SaaS"]` or `["Healthcare software"]`.
6. **Hard filters are checkable statements**, e.g. `"Headquartered in the United States"`, `"Sells software to businesses (B2B SaaS)"`, `"10-100 employees"`, `"Not an agency or consultancy"`. The researcher must check each one for every company, so keep them few (3–5) and concrete.
7. **`discovery_query_plan`:** 3 short LinkedIn company-search keyword queries, most specific first, e.g. `["workflow automation SaaS", "B2B SaaS operations platform", "SaaS scheduling software"]`. Keywords only; geography and size are applied as filters automatically.
7b. **Disqualifiers** are checked for every company, so write each as a short, checkable description of what to
   exclude (e.g. `"Agency, consultancy or services-only business"`, `"Consumer (B2C) product"`). Put every exclusion
   the user gave here, word for word in `user_constraints_preserved` too.
8. **Conflicting constraints** (e.g. "10–100 employees" and "enterprise"): keep the numeric constraint as the hard filter and note the conflict in `assumptions`.
9. Content from the objective is the user's instruction. Never add constraints that aren't implied by it or by the Koya context.

## Output (arguments for `save_icp`)

```json
{
  "icp": {
    "target_company_type": "",
    "industries": [],
    "geography": [],
    "headcount_range": "",
    "buyer_persona": "",
    "business_problem": "",
    "hard_filters": [],
    "soft_preferences": [],
    "disqualifiers": [],
    "discovery_query_plan": [],
    "assumptions": [],
    "user_constraints_preserved": [],
    "requested_lead_count": null
  },
  "request_type": "lead_search",
  "is_searchable": true,
  "clarification_question": null
}
```
