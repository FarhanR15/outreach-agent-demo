## Objective
Find candidate leads matching the proxy ICP, and get real contact details for them.

## Inputs
- `context/icp.md` — the filter reasoning (filters live in `settings.json`)
- `--limit N` — how many leads to end up with (default 5)
- `APOLLO_API_KEY` (from `.env`)

## Tools used (in order)
1. `tools/scrape_apollo.py` → Apollo search + enrichment → `.tmp/apollo_raw.json`

## Why this is two API calls, not one

Apollo splits search and contact data, confirmed 2026-08-20 against docs.apollo.io:

| | endpoint | cost | returns |
|---|---|---|---|
| Search | `POST /api/v1/mixed_people/api_search` | free | `id`, `first_name`, `last_name_obfuscated` ("Do***e"), `title`, `has_email`, `organization.name` |
| Enrich | `POST /api/v1/people/bulk_match` (max 10/call) | ~1 credit/person | real `email`, `last_name`, `linkedin_url`, `organization.primary_domain` |

Apollo's own docs: *"This endpoint doesn't return email addresses or phone numbers."*
So search alone produces unusable leads — the whole rest of the pipeline needs the
email and LinkedIn URL that only enrichment provides.

## Steps
1. Search with the ICP filters, as **query-string** params (Apollo takes these in the
   query string, not a JSON body), paginating until `--limit` candidates are collected.
2. Keep only candidates where `has_email` is true — no point spending a credit on a
   record that can't yield an email.
3. Enrich those ids via `bulk_match` in chunks of 10 (the API's hard cap).
   `reveal_phone_number` stays false: it costs 8 extra credits and needs a webhook.
4. Build `Lead` objects from the enrichment response; drop anything still without a
   usable email.
5. Filter out anyone already in the ledger (previously pushed or previously rejected).
6. Write `.tmp/apollo_raw.json`.

## Expected output
`.tmp/apollo_raw.json` — leads with real email, name, title, company domain, LinkedIn
URL. `verified=false` until the next stage.

## Cost
Search free; enrichment ≈ 1 Apollo credit per lead. `--limit 5` ≈ 5 credits.
Watch `credits_consumed` in the `apollo.enriched_chunk` log line on the first real run
and confirm it matches expectations before scaling up.

## Edge cases & failures (update as we learn)
- **The placeholder-email trap**: Apollo's `/people/{id}` endpoint returns the literal
  string `email_not_unlocked@domain.com` instead of null for locked emails, which passes
  naive `if email:` checks. We never call that endpoint, and `_usable_email` guards the
  literal regardless.
- Rate limits: paid plans 200 search req/min; 429s carry `retry-after` and are retried
  with backoff. 4xx errors are NOT retried (a bad key stays bad, and retrying a POST
  could double-charge).
- `--mock` reads `.tmp/fixtures/apollo_raw.json` and spends nothing.
