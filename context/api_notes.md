# API Gotchas — empirically verified

Every item here cost a failed run to discover. All verified against the live APIs on
2026-08-20, not from documentation alone. **Read this before changing any API call.**

## Apollo.io

- **Search returns no contact details.** `POST /api/v1/mixed_people/api_search` returns
  `id`, `first_name`, `last_name_obfuscated` ("Do***e"), `title`, `has_email`, and
  `organization.name` — no email, no real last name, no LinkedIn URL, no company domain.
  You must call `POST /api/v1/people/bulk_match` (max 10 ids per call, ~1 credit each)
  to get anything you can actually contact.
- **Params go in the query string**, not a JSON body — except `bulk_match`, whose
  `details` array *is* a JSON body.
- **`q_keywords` is a phrase match, not a bag of words.** Measured with all other ICP
  filters applied:

  | q_keywords | matches |
  |---|---|
  | *(none)* | 883,985 |
  | `coaching` | 3,297 |
  | `coach` | 6,113 |
  | `online coaching` | 16 |
  | `coaching course program mentorship` | **0** |

  Multi-word values silently return zero, which looks exactly like "no leads today"
  rather than a bug. **Keep it to one word.** `scrape_apollo.py` logs `total_entries`
  and warns on zero for this reason.
- **The placeholder-email trap**: `GET /api/v1/people/{id}` returns the literal string
  `email_not_unlocked@domain.com` instead of null for locked emails — it passes
  `if email:` checks. We never use that endpoint; `_usable_email()` guards it anyway.
- Search is free (0 credits); enrichment is ~1 credit/lead. Never set
  `reveal_phone_number=true` — 8 extra credits and it requires a webhook.

## Apify

- **Token goes in the `Authorization` header, not the `?token=` query param.** httpx puts
  the full URL in exception messages, so a query-string token gets copied verbatim into
  your logs. This actually happened during testing.
- **The two LinkedIn actors take different input keys.** They're from the same publisher,
  which makes this easy to get wrong:
  - `harvestapi/linkedin-profile-scraper` → `{"queries": [url], "profileScraperMode": ...}`
  - `harvestapi/linkedin-profile-posts` → `{"targetUrls": [url], "maxPosts": N}`
- **Do NOT pass `postedLimit` to the posts actor.** Passing `"6months"` returned 0 posts
  for a profile that returns 5 without it.
- **The profile actor reports failure with HTTP 200.** A missing profile comes back as
  `{"element": null, "status": 404, "error": "Profile not found"}` in the dataset, not as
  an HTTP error — so it looks like a successful empty result unless you check for it.
- **Email lives in `emails` (plural), a list of objects**, not an `email` string:
  `[{"email": "...", "status": "risky", "qualityScore": 60, "validEmailServer": true}]`.
  `_best_email()` ranks by `qualityScore` and skips invalid servers.
- **Memory limits bite under concurrency.** `apify/website-content-crawler` requests 8GB
  per run by default; a 16GB account therefore fails with a **402** on the third
  concurrent lead. We pin it to 2GB and use `crawlerType: "cheerio"` (no browser needed
  for text).
- Useful bonus field: the profile actor returns `followerCount`, a real audience-size
  signal for ICP judgement.

## Instantly

- **`America/New_York` is not a valid timezone** — the API accepts only a fixed enum and
  rejects anything else with a 400. `America/Detroit` is the Eastern-time entry that
  works.
- **`email_list` takes email address strings**, not account UUIDs (unlike the sibling
  `email_tag_list`, which does take UUIDs).
- Campaign `status` is read-only on create and comes back as `0` (Draft). Sending starts
  only via the separate `/activate` endpoint, which this project never calls.
- Status enum: `0` Draft, `1` Active, `2` Paused, `3` Completed, `4` Running
  Subsequences, `-99` Account Suspended, `-1` Accounts Unhealthy, `-2` Bounce Protect.
- `/campaigns/{id}/sending-status` returns `diagnostics: null` for a draft campaign —
  don't assume the object is populated.
- Leads upload in batches of up to 1000 via `POST /api/v2/leads/add`, with a
  first-class `personalization` field and `skip_if_in_workspace`.
- Rate limits are workspace-wide and shared across API v1 and v2: 100 req/sec,
  6,000 req/min.

## OpenRouter

- **Reasoning models spend most of `max_tokens` on thinking before emitting any
  output.** A critic with `max_tokens=300` returned the truncated string `{"pass": true,`
  — which parsed as a failure and silently degraded to "critic unavailable", skipping the
  safety check entirely. Both JSON roles now use 1500. `call_role()` logs
  `agents.response_truncated` when `finish_reason == "length"` so this can't hide again.
- **Several current models reject `temperature`** (reasoning models, and Claude Sonnet 5
  on OpenRouter). `Role.temperature = None` means "don't send the parameter at all".
- **Structured-output support is per-endpoint, not per-model** — the same slug served by
  a different provider may ignore your schema. We send
  `provider: {"require_parameters": true}` to pin routing to endpoints that honour it.
- Prefer `response_format: {"type": "json_schema", ...}` over `{"type": "json_object"}`;
  the former enforces shape, the latter only guarantees valid JSON.
