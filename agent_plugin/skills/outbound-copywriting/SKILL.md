---
name: outbound-copywriting
description: Use when writing the 3-step cold email sequence and LinkedIn message for ONE qualified lead, using only the lead's stored research. Explains the copy rules, placeholders, limits, how to self-check with check_drafts and how to fill save_outreach.
---

# Outbound Copywriting

Source: `assets/outbound-copywriting-guide.md` (rules kept verbatim), plus project specifics.

## Required Output

For each qualified lead, generate a 3-step cold email sequence.

Each step should include:

- Subject line
- Email body
- Personalization note

You may also generate a short LinkedIn message if your application supports it. (This application does: it's required.)

## Copy Rules

- Use the company context gathered during research.
- Keep each email short and direct.
- Write like a person, not a promotion.
- Do not invent details about the company.
- Avoid fake urgency, exaggerated claims, and generic praise.
- Do not include personal email addresses unless the user provided them.
- Do not send outreach.

## Suggested Sequence Structure

### Email 1

Open with a relevant observation from the company context, connect it to the offer, and ask a low-pressure question.

### Email 2

Add another relevant angle, such as a workflow bottleneck, scaling challenge, or operational pattern that connects to AI automation support.

### Email 3

Keep the final follow-up brief. Invite a reply if the timing or fit is wrong.

## Personalization

Good personalization references evidence:

- Website positioning
- Product or service category
- Audience served
- Hiring or scaling signal
- Public workflow or operational clue

Weak personalization is vague:

- "Loved what you are building"
- "Your company looks impressive"
- "I saw your website"

## Quality Check

Before finalizing copy, check:

- Does each email mention a real company-specific detail?
- Can each claim be traced to source context?
- Is the ask clear?
- Is the tone calm and credible?
- Would a human want to review this before sending?

## Project rules

1. **Start with `get_lead`** for the domain. Use ONLY its `source_summary`, `fit_reasons`, `hard_filter_evidence` and discovery facts. Nothing else is known about the company.
2. **The offer (Koya Talent):** we connect founders and operators with trained AI automation assistants who take over repetitive workflows and build AI-enabled internal systems. Never invent Koya pricing, customers, statistics, timelines or guarantees.
3. **Placeholders:** greet with `{{first_name}}` (we never know or guess the person's name) and sign off with `{{sender_name}}`. No other `{{...}}` placeholders.
4. **Limits (checked by code):** subject ≤ 60 characters; body ≤ 120 words; email 3 ≤ 80 words; LinkedIn message ≤ 300 characters (write about 40 words, ~250 characters, to leave room); **no email addresses, phone numbers or URLs** anywhere in the copy.
4b. **Template (checked by code):** steps numbered 1, 2, 3 with different subjects; email 1 ends with a low-pressure question; every email is signed `{{sender_name}}`; every email contains at least one company-specific fact from `get_lead` (the fact-checker confirms it is supported).
4c. **Write to the rules, then prove it:** `get_lead` returns `writing_rules`. Draft to them from the start, then call `check_drafts` (free, exact counts, no attempt used) and fix what it lists until `ok_to_save` is true. Only then call `save_outreach`.
5. **Banned phrases** (rejected automatically): "loved what you are building", "your company looks impressive", "I saw your website", "came across your company", "hope this finds you well", "act now", "limited time", "last chance", "don't miss out", "guaranteed", "revolutionary", "game-changer", "10x", "skyrocket", "urgent".
6. **`evidence_ref`:** for each email, the source URL its company observation came from. It must be one of the lead's `source_urls`.
7. **Hedge inferences.** If you connect a fact to a likely bottleneck, phrase it as a question or possibility ("with 40 clinics onboarding a month, is setup still manual?"), never as a claimed fact about their operations.
8. **After `save_outreach`:** if it returns problems (unsupported claims, or an email with no company-specific detail), rewrite only what it flags, re-check with `check_drafts`, and call it again. You get at most the number of rewrites the tool states; after that, stop.
9. The drafts are labelled DRAFT and reviewed by a human before anything is sent. Never suggest they were sent.

## Output (arguments for `save_outreach`)

```json
{
  "domain": "acme.io",
  "emails": [
    {"step": 1, "subject": "", "body": "Hi {{first_name}}, ...", "personalization_note": "", "evidence_ref": "https://acme.io/"},
    {"step": 2, "subject": "", "body": "", "personalization_note": "", "evidence_ref": ""},
    {"step": 3, "subject": "", "body": "", "personalization_note": "", "evidence_ref": ""}
  ],
  "linkedin_message": ""
}
```
