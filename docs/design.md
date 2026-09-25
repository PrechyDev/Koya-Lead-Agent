# design.md — UI rules (functional over pretty)

Goal: anyone can tell **what is clickable, what is happening, what went wrong, and what the results are**, without being told. Past feedback we are fixing:

1. Buttons gave no hover/click feedback, so people couldn't tell what was clickable.
2. System messages (e.g. AI scoring/status) appeared in the same box as the generated results.

---

## 1. The three zones rule (fixes #2)

Every screen keeps these **physically separate**, with their own visual style:

| Zone | Contains | Looks like | Never contains |
| --- | --- | --- | --- |
| **System Message bar** (top of the content area, sticky) | status, errors, warnings, success confirmations, "run in progress" | full-width coloured banner with an icon + text + optional action button | lead data or drafts |
| **Controls** | forms, buttons, filters, tabs | standard inputs | results or status text |
| **Results** | ICP, leads, drafts, tool-call log | white cards on a grey page | status/error text. If a section has no data, show an *empty state* inside it, not an error |

Per-item metadata (confidence score, grounding result, qualification status) is shown as **labelled badges/fields beside the content**, never mixed into the draft text. For example, an email card shows `Subject`, `Body`, then a separate grey "Personalization note" box and a "Grounding: ✓ all claims supported" badge in the card header. The body text box contains only the email.

Banner types (always icon + colour + words, never colour alone):

| Type | Colour token | Icon | Example |
| --- | --- | --- | --- |
| info | `--info` | ℹ | "Run started. This usually takes 5–15 minutes. You can leave this page." |
| success | `--success` | ✓ | "Run completed: 10 qualified leads." |
| warning | `--warning` | ⚠ | "Completed with 7 of 10 leads. Reason: …" |
| error | `--danger` | ✕ | "Discovery failed: Apify timed out. No charges beyond 1 run. [Retry]" |

Error messages say **what failed · why (if known) · what to do next**. Never show raw stack traces. Show `code` in small grey text for debugging.

---

## 2. Button & interactive states (fixes #1)

**Every** clickable element must have all of these. No exceptions (buttons, links, tabs, table rows, chips, copy icons).

| State | Rule |
| --- | --- |
| Default | clearly a button: filled background (primary) or 1px border (secondary); `cursor: pointer` |
| Hover | background darkens ~10% (`--primary-hover`); rows get `--row-hover` bg; underline for text links |
| Focus (keyboard) | 2px `--focus` outline with 2px offset (`:focus-visible`) |
| Active (pressed) | darker still + `transform: translateY(1px)` |
| Disabled | `--disabled-bg`, `--disabled-text`, `cursor: not-allowed`, and a **tooltip/helper text saying why** ("A run is already in progress") |
| Loading | spinner + verb-ing label ("Starting…", "Exporting…"); disabled while loading so it can't be double-clicked. **Starts only on a validated submit, never on click** (a click fires before the browser checks required fields) |
| Required inputs | **A submit button is disabled until every required field in its form is valid.** Problems are shown **like Google Forms: in red directly under the field, with a red border, while typing** (after a ~0.6 s pause, and at once on leaving the field), and they vanish as soon as the value is fixed: "Enter a valid email address, like name@company.com.", "Use at least 8 characters.", "The passwords don't match.", "Describe the companies in at least 5 words." An empty field shows nothing (people can see it). Not a hover tooltip: tooltips don't appear on phones and are easy to miss. A button with its own condition declares it (`data-requires="<field id>"`, e.g. Reject needs a note, whose label says so). Handled once for every form, including HTMX-loaded ones, in `app/static/app.js` |
| Email fields | `name@domain.tld`: at least one dot after the @ and a final part of 2+ letters (`sam@acme` is rejected; `sam@mail.acme.co.uk` is fine). One rule, `app/lib/validation.py`, used as the field's `pattern` **and** checked again on the server |
| Done feedback | short confirmation in place, e.g. Copy → "Copied ✓" for 2s; Approve → button turns into an "Approved ✓" badge + success banner |

Button hierarchy: **one primary** button per view (e.g. "Start research run"). Secondary = outlined. Destructive (Cancel run, Reject) = red outline, and it needs a confirm dialog.

Clickable table rows show a `›` chevron on the right and the hover background. Non-clickable text never gets hover styles.

---

### How to get these states with Jinja + HTMX (no JS framework)

| Need | How |
| --- | --- |
| Hover/focus/active/disabled | plain CSS on `.btn`, `.btn:hover`, `.btn:focus-visible`, `.btn:active`, `.btn:disabled` |
| Loading + no double-submit | HTMX forms: `app.js` disables **every** button of the form while it sends and afterwards re-enables exactly those it disabled (so buttons the server disabled stay disabled); `hx-disabled-elt="find button"` is not used because htmx only takes the first match (D-68). The `.htmx-request` class shows the spinner (`.btn .when-loading`). The New run form keeps `hx-disabled-elt="#start-run"`. Plain forms (login, accept invite): `app.js` adds `.is-loading` on the `submit` event |
| Server errors → banner, not results | the server answers errors with the `HX-Retarget: #system-message` + `HX-Reswap: innerHTML` headers and renders `partials/banner.html`. htmx 2 ignores 4xx/5xx by default, so `app.js` (`htmx:beforeSwap`) swaps in error responses **that carry `HX-Retarget`**; any other error response is still ignored. So errors can **never** land inside a results block |
| Live run updates | `hx-get="/runs/{id}/live" hx-trigger="every 3s"`; the server returns **HTTP 286** once the run is terminal, which stops polling |
| Copied ✓ feedback | a tiny inline script: `navigator.clipboard.writeText(...)`, then swap the label for 2s (plus a fallback that selects the text) |
| Confirm destructive actions | a form with one button: `hx-confirm="Cancel this run? Work done so far is kept."` on the form. A button in a form with several buttons: `data-confirm="…"` on the **button** (htmx reads `hx-confirm` from the form, not the clicked button; `app.js` asks before htmx submits) |

## 3. Design tokens (top of `app/static/app.css`; a dark-mode set follows under `prefers-color-scheme: dark`)

```css
:root {
  --bg: #f5f6f8; --surface: #ffffff; --surface-2: #eef1f5; --border: #d9dce1; --text: #1b1f24; --text-muted: #5c6470;
  --primary: #2350d8; --primary-hover: #1b3fae; --primary-active: #153287; --on-primary: #ffffff;
  --focus: #f0a500;
  --success: #1f7a3a; --success-bg: #e6f4ea;
  --warning: #8a5a00; --warning-bg: #fff4d6;
  --danger:  #b3261e; --danger-bg:  #fdecea;
  --info:    #1d4f91; --info-bg:    #e8f0fb;
  --neutral-badge: #e9ebee;
  --row-hover: #eef2fd;
  --disabled-bg: #e3e5e8; --disabled-text: #8a9099;
  --radius: 6px; --space: 8px;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "Cascadia Code", Menlo, monospace;
}
```
Contrast: text/background ≥ 4.5:1. Base font 15px, line-height 1.5. Spacing in multiples of 8px.

Status badge mapping (use everywhere):

| Status | Badge |
| --- | --- |
| qualified | green bg `--success-bg`, text `--success`, "✓ Qualified" |
| needs_review | amber `--warning-bg`, "⚠ Needs review" |
| not_qualified | grey `--neutral-badge`, "✕ Not qualified" |
| pending | outlined grey, "… Pending" |
| tool call success / error / blocked | ✓ green / ✕ red / ⛔ amber |

---

## 4. Screens

### 4.1 Runs (`/`) and New run (`/runs/new`)
- **Runs** is the history page: title + **[+ New run]** (primary) at the top; status filter chips with counts (All · In progress · Waiting for you · Completed · Partial · Failed · Cancelled); a search box for objectives and an "Only my runs" checkbox; the table (objective · status badge · qualified/target · by · started · ›); "Showing 21–40 of 57" with [← Previous] [Next →] (disabled at the ends). Empty states: "No runs yet. Start your first run" / "No runs match these filters. Clear filters". Every filter is part of the URL, so views can be bookmarked and Back works.
- **New run** is the form on its own page (← All runs): objective box (5+ words; message under the box), example chips (Broad, Specific), "What we assume when you leave something out", admins see a Test mode line while test limits are on, [Start research run]. No cost limits or budget here.
- **Waiting for you** (repeat check, clarification): a full-screen dialog over the run page. The main choice has focus; ✕, [Cancel and edit my objective] and Esc go back to New run with the objective filled in. The progress bar shows the step as "waiting for you" (!) instead of spinning.

### 4.2 Run page (`/runs/:id`)
```
├ System Message bar: current status sentence / error / completion ┤
│ Objective (quoted, read-only)                     [Cancel run]  │
│ Stepper: Refine ICP ▸ Discover ▸ Scrape ▸ Qualify ▸ Draft ▸ Done │
│ Counters: Companies 12/20 · Pages 8/20 · Qualified 4/10 · $0.84/$1.25 │
│ Tabs: [ICP] [Leads (14)] [Tool calls (37)] [Summary]            │
│ ─ tab content (Results zone) ─                                  │
```
- Stepper: done = green check, current = blue with spinner, pending = grey, failed = red ✕ at the failing step.
- Polling every 3s while non-terminal. Show "Last updated 2s ago" in small muted text. Stop polling at a terminal status.
- **ICP tab:** the table of fields; hard filters and soft preferences as separate lists; "Assumptions the agent made" in a highlighted box; "Your constraints preserved" list.
- **Leads tab:** filter chips (All / Qualified / Needs review / Not qualified / Pending) with counts; table: company · domain (external link ↗, opens a new tab) · status badge · fit score ("0.80") · drafts status · review status. Row click → detail drawer. Download CSV / sample pack (JSON) buttons sit above the table.
- **Tool calls tab:** chronological table: # · tool · purpose · input summary · result summary · status badge · duration. Error rows are tinted red with the error message visible (not hidden in a tooltip). Filter by tool/status.
- **Summary tab:** run summary, shortfall reason, costs, turns, and export buttons [Download CSV] [Download JSON].

### 4.3 Lead detail drawer (right side, ~50% width; full screen on mobile)
Sections in order, each with a heading:
1. **Header:** name, domain ↗, status badge, fit score, drafts status, review status (+ reviewer name).
2. **Why (qualification):** hard-filter checklist (✓/✕/? per filter + evidence + source link); "How the fit score was calculated" (each +/− item, computed by the system); exclusions (applies / does not apply / unknown); nice-to-haves; tools seen on the site; fit reasons; concerns (amber box); the pre-screen result.
3. **Sources:** source URLs (links) + source summary. Injection flags, if any, in a warning box.
4. **Outreach drafts:** label "Draft · not sent · requires human review". Three cards, **Email 1/2/3**. Each card: Subject (with copy), Body (with copy), a separate "Personalization note" + "Evidence: <url>" box, and a fact-check badge. Then the LinkedIn card with a char count "212/300", and a collapsible "Fact-check details" list of every checked claim.
5. **Review actions (sticky footer):** reviewer note textarea · [Approve drafts] (primary) · [Reject] (red, **disabled until a note is written**, then asks to confirm) · [Undo approval] once approved.
- Close with ✕, the Esc key, or a backdrop click. Focus returns to the row.

### 4.4 Login, invite, team & spend
- **Login:** a centred card with email, password and [Sign in] (loading state "Signing in…"). Errors go in the banner ("Email or password is incorrect"). No sign-up link: "Access is by invitation. Ask your admin."
- **Accept invite:** a name + password (with confirm) form. Rules are shown before typing, not only after an error.
- **Password fields** (everywhere) have an eye button inside the box to show/hide what's typed.
- **Forgot password:** "Forgot password?" under the Sign in button → email → [Send reset link] → the same neutral confirmation for everyone ("If this email has access, a reset link is on its way"). **Choose a new password:** opened from the email; new password + confirm; locked with an explanation if opened without the email link. Team page: [Send reset link] per active member (confirm first).
- **Header:** app name · **+ New run** first (every page, D-93/D-95) · Runs · (admin) Team · (admin) Spend · (developer) System issues with an open-issue count · the user's name, role badge and Sign out. Developers also get a banner on every page while an issue is open (D-90).
- **Team (admin):** a table of name · email · role badge · status (Active/Deactivated) · actions. [Invite teammate] is the primary button. Deactivate needs a confirm and is disabled, with a tooltip, for the owner or the last admin. The result of inviting an existing account is shown as an info banner: "They already have an account. They can sign in with their existing password."
- **Spend (admin):** budget bar ($ spent / $6.00), then tables by run, model and source (icp / run / grounding / preflight / eval), plus Apify cost.
- **System issues (developer, D-90):** open and resolved alerts, each with what happened and how to fix it; [Mark fixed]; [Send a test alert] (disabled, with the reason, until the n8n webhook is set).

### 4.5 Repeat-objective gate
- **Stage 1 (form):** as you type (debounced), an info hint appears *under the objective field*: "You ran this on Sep 20: 10 qualified. [View results]". It's informational; Start stays enabled.
- **Stage 2 (run page, status `awaiting_confirmation`):** a **full-screen dialog** (D-67) says the run matches an earlier one (with a link and its qualified count), with the choices [Open it] (primary) · [Find new companies] · [Refresh the same companies], each with a one-line explanation, and ✕ / [Cancel] that goes back to the New run form with the objective filled in. The page behind is locked, the main choice has focus, Esc = ✕. The progress bar shows the step as "waiting for you".

### 4.6 Clarification state
When status = `needs_clarification`, a **full-screen dialog** (D-67) shows the agent's question, an answer box (focused) and [Continue with this answer]. This creates a linked new run, and the old run becomes "Replaced" with a link to the follow-up run (D-73).

---

## 5. Empty, loading & error states

| Situation | Show |
| --- | --- |
| No runs yet | "No runs yet. Start your first run" (links to New run) |
| Tab data not yet produced | a muted placeholder: "Leads appear here once discovery finishes." |
| Initial page load | pages are server-rendered, so the full layout arrives with the first response (no blank screen, no skeletons needed) |
| Server unreachable (e.g. Render waking up, Wi-Fi drop) | error banner "Can't reach the server. Retrying automatically…" + [Retry now]; live polling keeps retrying and the banner clears on the next success (`app.js`) |
| Run failed | error banner with message + step; the partial results stay visible below |

---

## 6. Copy & tone
Plain words. Say "companies", "sites scraped", "qualified". Don't say "MCP", "tool_use" or "RLS" in the UI (the Tool calls tab may show tool names). Numbers always show their limit ("8 / 22").

---

## 7. Responsive & accessibility
- Works at 360px wide: a single column; the drawer becomes full screen; tables scroll horizontally *inside their card* only.
- All inputs have visible `<label>`s. Icons have `aria-label`. The banner region is `role="status"` (info/success) or `role="alert"` (error).
- Everything is keyboard reachable in a logical order.

---

## 8. Definition-of-done checklist (check every new page against it)

- [ ] Every button/link/row/chip/tab has hover, focus-visible, active, disabled and (where async) loading states
- [ ] No submit button is clickable while a required input is empty or invalid; invalid (not empty) input says why
- [ ] Disabled controls explain why
- [ ] No status/error/score text inside result content boxes; metadata is in badges/labelled fields
- [ ] One System Message bar per page, correctly coloured + iconed, with `role` set
- [ ] Error messages state what · why · next step
- [ ] Destructive actions confirm
- [ ] Copy buttons show "Copied ✓"
- [ ] Drafts labelled "DRAFT — not sent"
- [ ] Empty and loading states on every list/tab
- [ ] Usable at 360px and keyboard only
- [ ] No secrets or tokens in the page source; the login password field is `type="password"`
- [ ] Admin-only nav items (Team, Spend) are hidden for members, and the server refuses them anyway
- [ ] "Approved by <name> · <time>" is shown on reviewed leads
- [ ] Repeat gate: the stage-1 hint sits beside the form (not in results); the stage-2 pause shows 4 clearly labelled buttons, one primary ("Open it")
- [ ] Cold-start friendly: the first page load shows the layout immediately (server-rendered). No blank screen while Render wakes up
- [ ] Spend is visible **to admins only** (D-66): the Run page and Summary tab show Claude $ used / cap and model names to admins; the Spend page shows project spend / $6.00. Members never see AI costs

## Themes and row menus (D-95)

- **Theme switch** in the header on every page (also signed out): Auto (follows the device) → Light → Dark, remembered per browser (`localStorage`). Colours are tokens in `:root`; dark overrides apply under `prefers-color-scheme: dark` unless Light is chosen, or whenever Dark is chosen.
- **Row actions:** one **Manage ▾** menu per row when a row has more than two actions. Disabled items stay visible with the reason underneath; destructive items are red and last, after a divider.
- **Tables:** numbers right-aligned; dates and names never wrap; long text clamps to 2 lines with the full text on hover; clickable rows end in a › column.
