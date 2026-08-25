## Objective
Create a new, distinctly-named Instantly campaign in DRAFT/PAUSED state, load the
personalized leads into it, and configure the 3-step reply-aware sequence. Never
activate sending.

## Inputs
- `.tmp/leads_personalized.json` — output of `personalize_copy.py`
- `context/messaging.md` — the 3-step sequence templates + wait-day spacing
- `context/compliance.md` — unsubscribe handling, daily send cap
- `INSTANTLY_API_KEY`, `INSTANTLY_SENDING_EMAILS` (from `.env`)

## Tools used (in order)
1. `tools/push_to_instantly.py` → Instantly v2 API (`api.instantly.ai`):
   `POST /api/v2/campaigns` to create, `POST /api/v2/leads/add` to upload leads

## Steps
1. Load `.tmp/leads_personalized.json`.
2. Create a campaign named `Demo - Apollo Coaches - {YYYY-MM-DD}` — the date-stamped,
   distinctly-prefixed name is how we guarantee this never gets confused with (or
   accidentally merged into) any other campaign in the same workspace. `status`
   is read-only on this endpoint; new campaigns come back as `0` (Draft) since sending
   only ever starts via the separate activate/resume endpoint, which this tool never
   calls.
3. Assert the response's `status` is `0` (Draft) — hard-fail loudly if it's ever
   anything else, rather than proceeding silently.
4. Upload all leads in a single batched `POST /api/v2/leads/add` call (up to 1000 per
   call) with `skip_if_in_workspace: true` — this is the real safeguard against ever
   re-adding a lead that already exists anywhere else in the Instantly workspace,
   including campaigns this tool never created. Each lead's `custom_opener` maps
   to Instantly's first-class `personalization` field (not a custom variable).
5. Configure the 3 sequence steps from `context/messaging.md` (subject/body per step,
   wait-days between) at campaign-creation time, with `stop_on_reply: true` and
   `insert_unsubscribe_header: true` (Instantly's real unsubscribe mechanism — a header,
   not a body merge tag; see `context/compliance.md`).
6. Set the daily send limit from `context/compliance.md` (respect existing mailbox
   warm-up — don't blast the full batch on day one).
7. Log a clear final message: campaign name, lead count, and "review in Instantly and
   click Launch when ready."

## Expected output
A paused Instantly campaign, fully configured, visible in the Instantly dashboard.
Nothing sent.

## Edge cases & failures (update as we learn)
- Any lead missing a valid email at this point: exclude from upload, log it — should be
  rare since verify_apify.py already checked deliverability.
- Instantly API errors on lead upload for a subset: retry once (tenacity), then log and
  continue with the rest rather than failing the whole push.
- `--mock` mode logs what it *would* create/upload without calling the Instantly API at
  all — useful for reviewing sequence copy before spending a real API call.
