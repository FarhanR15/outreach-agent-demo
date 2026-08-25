# CLAUDE.md — Outreach Agent

This file provides guidance to Claude Code when working in this repository.

## Conventions

This project follows the **WAT** convention:

- **W**orkflows — markdown SOPs in `workflows/`, one per pipeline stage
- **A**gent — Claude orchestrates, never hand-transforms data a tool exists for
- **T**ools — deterministic Python in `tools/`, async/await, Pydantic v2, `structlog`
  (never `print`), secrets loaded from `.env` via `python-dotenv`

## What This Project Is

A cold-outreach pipeline that sources leads, researches them, drafts one personalized
opening line each, and stages them for human review before anything is sent:
**Apollo.io → Apify → OpenRouter → Instantly**.

```
1. SCRAPE      → Apollo search (free) then Apollo bulk_match enrichment (~1 credit/lead).
                 TWO calls, because search alone returns no email, no last name and no
                 LinkedIn URL. Filtered on proxy criteria for the target ICP (see
                 context/icp.md — Apollo has no field for revenue or audience size, so
                 it's a proxy, refined at the verify step)
2. VERIFY      → Apify actors: LinkedIn profile (+ email discovery when Apollo has none),
                 recent posts, and company website. Confirms a real person with a real
                 active business matching ICP intent; bad fits dropped with a reason
3. PERSONALIZE → OpenRouter LLM writes ONE opener sentence per lead from that research,
                 preferring something they recently posted about. Guardrails reject
                 fabricated numbers and fall back to a bland-but-safe line
4. REVIEW      → Local dashboard (tools/dashboard.py). You read every lead and its
                 opener, edit or reject, and approve what you want contacted. NOTHING
                 reaches Instantly without passing through here.
5. DELIVER     → Approved leads only → a new distinctly-named Instantly campaign in DRAFT
                 state. You click Launch yourself. Instantly's own lead list is the
                 system of record.
```

## Never Contact Anyone Twice

Three independent layers, because this is the failure that burns a domain and a
reputation:

1. **The ledger** (`.tmp/ledger.json`) — a permanent record of everyone ever pushed or
   rejected. `scrape_apollo.py` filters against it, so nobody is even researched twice.
2. **`skip_if_in_workspace: true`** on the Instantly lead upload — Instantly itself
   refuses a lead that exists anywhere else in the workspace, including campaigns this
   tool never created.
3. **`pushed_to_campaign`** on each lead — the dashboard locks already-pushed leads and
   `push_to_instantly.py` filters them out.

Keep all three. They cover different failure modes (local state loss, a second campaign,
a re-run mid-review).

## Directory Layout

```
workflows/     # one markdown SOP per pipeline stage (Objective/Inputs/Tools/Steps/
               # Expected output/Edge cases format)
tools/         # Python: scrape_apollo.py, verify_apify.py, personalize_copy.py,
               # push_to_instantly.py, run_pipeline.py, dashboard.py + dashboard_ui.html,
               # _common.py (shared models, ledger, retry, concurrency helpers),
               # config.py (settings store — every tunable the dashboard can edit)
settings.json  # the tunables themselves: Apollo filters, the three agent roles and their
               # prompts, Apify actors and limits, sequence copy. Written by the dashboard,
               # safe to hand-edit, NEVER holds secrets. Absent = built-in defaults.
context/
  icp.md          # Apollo proxy filters + the mapping assumptions made explicit
  messaging.md    # 3-step sequence copy templates + personalization variable spec
  compliance.md   # unsubscribe/CAN-SPAM, per-mailbox daily send cap, opt-out handling
  api_notes.md    # verified API behaviours and the traps found along the way
.tmp/
  leads_run.json  # ONLY the current batch — replaced each run
  ledger.json     # permanent: everyone ever pushed or rejected
  mock/           # a complete separate copy of both, for --mock runs
  fixtures/       # sample data for mock runs
```

## Commands

```
cd tools
python run_pipeline.py --mock --limit 3   # full pipeline, zero API calls, zero cost
python run_pipeline.py --limit 5          # real run: scrape + research + personalize
python dashboard.py                       # review UI at http://127.0.0.1:8000
```

**Where settings live.** Every tunable — Apollo filters, agent models/prompts, Apify
actors, sequence copy, daily cap — is in `settings.json`, edited from the dashboard's
Configure screens and read through `tools/config.py`. Precedence is: `settings.json` →
the matching `.env` variable → the defaults in `config.DEFAULTS`. Do NOT reintroduce
these as constants in the tools; a constant cannot be changed from a browser.

`context/icp.md` and `context/messaging.md` remain the **rationale** for those values —
why the Apollo proxy is what it is, what the sequence is trying to do. Read them before
changing filters or copy; change the values in the dashboard, and record the reasoning
back in those files.

## Operating Rules

- **Never call Instantly's launch/activate/start-sending endpoint.** `push_to_instantly.py`
  creates the campaign and loads leads in a paused/draft state only. A human clicks Launch
  in the Instantly UI — that is the send gate for this project.
- **Namespace the campaign.** This project's Instantly campaign is distinctly named
  (`Demo - Apollo Coaches - {date}`) so it is never confused with any other campaign in
  the same workspace.
- **Never read, write, or expose `.env`.** Only `.env.example` (variable names + a
  one-line comment on source) is ever committed. You fill in real keys yourself.
- **Reply detection and follow-up branching are native Instantly features** (sequence
  steps with wait-days, auto-stop-on-reply, Unibox). Don't build custom reply-parsing —
  just configure the sequence correctly via the Instantly API.
- Every tool supports `--mock` (runs against `.tmp/fixtures/` with zero API calls) and
  `--limit N` (caps how many leads flow through, for cheap real-API test runs).
- Paid calls (Apollo, Apify, OpenRouter, Instantly) — confirm before scaling a run past
  the first `--limit 5` validation batch.

## Validation Gate

Before scaling beyond a 5-lead test batch: review the actual leads, verification notes,
and personalized openers for that batch. If the ICP proxy in `context/icp.md` is
producing bad fits (per verify-step drop reasons in the logs), stop and tune the Apollo
filters before increasing volume.
