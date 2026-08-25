# Messaging — Sequence Copy & Personalization Spec

> **Live values are in `settings.json`**, edited on the dashboard's Campaign screen.
> This file is the reasoning behind them — update it when you change them, so the
> *why* does not get lost in a JSON file.


**Status: DRAFT.** These are placeholder templates so the pipeline is runnable
end-to-end. Rewrite them in your own voice before any real batch goes past the 5-lead
test — voice fidelity matters more than clever copy. A draft that sounds like you and
gets edited lightly is a win; a "better" draft in the wrong voice is a failure.

## Personalization Variable Spec

Confirmed 2026-08-20 against Instantly's API docs — `{{personalization}}` is a
first-class Instantly lead field (not a custom variable), which is where
`custom_opener` from `personalize_copy.py` lands. Casing below (`firstName` etc.)
matches Instantly's own example; double-check against the merge-tag picker in the
Instantly campaign editor before the first real send.

- `{{firstName}}` — from Apollo/Apify
- `{{personalization}}` — one sentence from `personalize_copy.py`, referencing
  something real and specific about their business (never a fabricated claim)
- `{{companyName}}` — from Apollo/Apify

## Sequence

Bodies are HTML (`<br/>` for line breaks — that's what Instantly's API expects, see
`tools/push_to_instantly.py`). Unsubscribe is handled two ways: Instantly inserts a
List-Unsubscribe header automatically (`insert_unsubscribe_header: true`, set in the
push script), plus a plain "Reply STOP" line in the body as a visible courtesy for
clients that don't surface the header.

### Step 1 — Day 0

**Subject:** quick one for {{firstName}}

**Body:**
```
Hi {{firstName}},

{{personalization}}

[One or two lines on who you are and the specific outcome you deliver. Replace this
before your first real send — placeholder copy is the fastest way to burn a good lead.]

Worth a quick look at what that could mean for {{companyName}}?

Reply STOP at any time to opt out.
```

### Step 2 — Day 3 (only if no reply)

**Subject:** re: quick one for {{firstName}}

**Body:**
```
Hi {{firstName}} — following up in case this got buried.

[Offer one concrete, low-friction next step — an example, a teardown, a relevant
result. No pressure either way.]

Reply STOP at any time to opt out.
```

### Step 3 — Day 7 (only if no reply)

**Subject:** last one from me

**Body:**
```
Hi {{firstName}},

I'll leave it here — if scaling paid acquisition becomes a priority later, feel free to
reach out any time.

Reply STOP at any time to opt out.
```

## Fallback Opener (used when personalize_copy.py can't produce a safe custom line)

"I came across {{companyName}} and wanted to reach out directly."
