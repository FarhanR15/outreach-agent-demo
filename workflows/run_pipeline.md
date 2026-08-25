## Objective
Run scrape → verify/research → personalize, then **stop for human review**. Pushing to
Instantly is a separate, deliberate act.

## Inputs
- `--limit N` — how many leads to pull (default 5)
- `--mock` — run the whole thing against fixtures, zero API calls, zero cost
- All env vars listed in `.env.example`

## Tools used (in order)
1. `tools/scrape_apollo.py` — Apollo search + enrichment
2. `tools/verify_apify.py` — verification + deep research
3. `tools/personalize_copy.py` — one opener per lead
4. *(stop)* — review in `tools/dashboard.py`, then push from there

## Steps
1. `require_env` up front — fail before spending anything if `.env` is incomplete.
2. Scrape. Stop if zero new leads (already-handled leads are skipped via the ledger).
3. Verify + research. Stop if zero survive — that's an ICP-filter problem, and pushing
   a near-empty batch is never the right move.
4. Personalize.
5. Print a summary including how many openers came from the model vs. fell back to the
   generic line — a high fallback count means the research is too thin to personalize
   from, which is worth knowing before you email anyone.

## Expected output
`.tmp/leads_run.json` (this batch only) plus a console summary. Nothing sent, nothing
in Instantly yet.

## The two-file model (this is what prevents double-contacting)
- `.tmp/leads_run.json` — **only the current batch**. Replaced every run. Every stage
  after scrape reads and writes this.
- `.tmp/ledger.json` — **permanent record** of everyone ever pushed or rejected, keyed
  by email/LinkedIn URL. Scrape filters against it, so nobody is researched or
  contacted twice across runs.
- `.tmp/mock/` — an entirely separate copy of both, so test data can never reach a real
  campaign.

## Edge cases & failures (update as we learn)
- A stage producing zero output is a stop condition, not a warning.
- `--limit` caps scrape AND verify, so a large leftover batch can't sneak through.
- Per-lead failures are dropped with a logged reason; only stage-level emptiness stops
  the run.
