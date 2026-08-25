## Objective
Confirm each Apollo candidate is a real person running a real, active business that
plausibly matches the coach/course-creator ICP, and enrich with profile/company detail
for personalization. Drop anything that doesn't hold up.

## Inputs
- `.tmp/apollo_raw.json` — output of `scrape_apollo.py`
- `APIFY_TOKEN`, `APIFY_LINKEDIN_ACTOR`, `APIFY_COMPANY_ACTOR` (from `.env`)

## Tools used (in order)
1. `tools/verify_apify.py` → Apify LinkedIn profile actor + company/website actor →
   `.tmp/leads_verified.json`

## Steps
1. Load `.tmp/apollo_raw.json`.
2. For each lead, run the Apify LinkedIn actor against `linkedin_url` (or name+company
   if missing): confirm the profile exists, is active, and current title/bio signals
   line up with "coach / course creator / online educator" intent.
3. Run the Apify company/website actor against `company_domain`: confirm the site is
   live, and pull any public signals (offer, price point mentions, social links) useful
   for personalization.
4. Validate email deliverability (format + MX check at minimum; Apify's email
   verification actor if available).
5. Dedupe against `.tmp/leads_verified.json` from any prior run in this project (by
   email) so re-running the pipeline doesn't re-verify or re-contact the same lead.
6. Any lead failing person-verification, company-verification, or email validity is
   dropped — log the lead identifier + reason via `structlog` (not silently discarded).
7. Write survivors to `.tmp/leads_verified.json` with enrichment fields populated.

## Expected output
`.tmp/leads_verified.json` — list of `Lead` objects with `verified=true`,
`verification_notes`, and enrichment fields (`company_summary`, `linkedin_bio_snippet`).

## Edge cases & failures (update as we learn)
- Apify actor run fails/times out for one lead: log and drop that lead, don't kill the
  batch.
- LinkedIn profile private/unreachable: mark `verified=false, reason="profile_unreachable"`,
  drop.
- If drop rate on a batch is high (>50%), that's a signal the Apollo ICP proxy filters
  in `context/icp.md` need tuning — surface this in the run summary, don't just silently
  proceed.
- `--mock` mode reads `.tmp/fixtures/apollo_raw.json` and fabricates verification results
  instead of calling Apify.
