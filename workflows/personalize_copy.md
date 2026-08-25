## Objective
Generate one personalized opener line per verified lead, used as a merge variable in
the fixed sequence templates — not a full email rewrite.

## Inputs
- `.tmp/leads_verified.json` — output of `verify_apify.py`
- `context/messaging.md` — voice notes + the personalization variable spec
- `OPENROUTER_API_KEY`, `OPENROUTER_PERSONALIZE_MODEL` (from `.env`)

## Tools used (in order)
1. `tools/personalize_copy.py` → OpenRouter chat completion, one call per lead →
   `.tmp/leads_personalized.json`

## Steps
1. Load `.tmp/leads_verified.json`.
2. For each lead, build a prompt from `company_summary` + `linkedin_bio_snippet` +
   the voice notes in `context/messaging.md`, asking for exactly one sentence — a
   specific, non-generic opener referencing something real about their business.
3. Call OpenRouter. Reject and fall back to a safe generic opener if the model returns
   something over ~30 words, includes a fabricated claim marker, or the call errors.
4. Attach the opener to the lead as `custom_opener`.
5. Write `.tmp/leads_personalized.json`.

## Expected output
`.tmp/leads_personalized.json` — same lead list, each with `custom_opener` populated
(either model-generated or the safe fallback).

## Edge cases & failures (update as we learn)
- OpenRouter call fails or times out for one lead: use the generic fallback opener from
  `context/messaging.md`, log it, continue — never block the batch on one bad call.
- Model output fails the sanity checks (too long, mentions unverifiable specifics like
  exact revenue figures): fall back to generic, don't push unverified claims into a
  cold email.
- `--mock` mode fabricates a placeholder opener instead of calling OpenRouter.
