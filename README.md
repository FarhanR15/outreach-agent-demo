# Outreach Agent — How It Works

Plain-English guide to the whole system. For API gotchas see `context/api_notes.md`;
for the rules Claude follows in this folder see `CLAUDE.md`.

---

## The one-paragraph version

You ask for N leads. The system finds coaches and course creators in Apollo, pays a
credit each to unlock their real email and LinkedIn, then researches each one properly —
their profile, their last five LinkedIn posts, and their website. Three different AI
models then work in sequence: one decides whether the person is actually worth
contacting, one writes a single opening sentence based on the research, and a third
independently checks that sentence for invented facts and generic filler. What survives
lands in a review dashboard on your machine. You read each one, edit anything that's off,
tick the ones you want, and push them into a **draft** Instantly campaign. **Nothing ever
sends until you click Launch in Instantly yourself.**

---

## Running it

```bash
cd tools

python run_pipeline.py --mock --limit 3   # practice run: no API calls, no cost
python run_pipeline.py --limit 5          # real run
python dashboard.py                       # the whole system at http://127.0.0.1:8000
```

The dashboard is now the front door: it runs the pipeline, reviews the leads, and edits
every setting — targeting, the three agent roles and their prompts, the research actors,
and the sequence copy. The CLI still works and does exactly the same thing.

Start with `--mock`. It exercises the entire pipeline against sample data and spends
nothing.

---

## The five stages

### 1. Find them — Apollo (`scrape_apollo.py`)

Apollo splits this into two calls, and the first one is nearly useless on its own:

- **Search** (free) returns a name, a job title, and a *deliberately obscured* last name
  like `Do***e`. No email. No LinkedIn URL.
- **Enrichment** (~1 credit per lead, batches of 10) turns those into real contact
  details.

So the cost is about **1 Apollo credit per lead**. We only pay it for people Apollo
already flags as having an email, so credits aren't wasted on dead ends.

Who it looks for: founders/CEOs/coaches at companies of 1–10 people in the US, UK,
Australia and Canada, with a verified email, matching the keyword "coaching". Those
filters live in `settings.json` (edited on the dashboard's Targeting screen), with
the reasoning in `context/icp.md`.

> **One trap worth knowing**: Apollo's keyword field matches the whole phrase, not
> individual words. `"coaching"` finds 3,297 people; `"coaching course program
> mentorship"` finds **zero**. Keep it to one word.

### 2. Research them — Apify (`verify_apify.py`)

For each lead, three scrapers run:

| What | Why it matters |
|---|---|
| LinkedIn profile | Confirms they're real, gets their bio and follower count, and finds their email if Apollo couldn't |
| Last 5 LinkedIn posts | **The most valuable input.** What someone posted last week is what makes an opener sound like a human actually looked |
| Their website (3 pages) | What they actually sell |

The posts and website run at the same time to halve the wait. Anyone we learn nothing
about gets dropped here — there'd be nothing to personalize from.

This stage is the slow one: roughly **20–40 seconds per lead**, five leads at a time.
That's Apify's speed, not something the code can fix.

### 3. Judge and write — three AI models (`personalize_copy.py`, `agents.py`)

This is the part that makes the outreach worth sending. Three different models, from
three different companies, each with one job:

**Qualifier** (`openai/gpt-5.6-luna` — cheap, runs on everyone)
Reads the research and decides: is this genuinely an online coach or course creator with
a paid offer? It rejected a freight-logistics VP, and a coach with no visible paid
program — both with a readable one-line reason. Rejects stop here, before we spend money
writing to them.

**Writer** (`anthropic/claude-sonnet-5` — the expensive one, and worth it)
Writes exactly one opening sentence. It is told to reference something specific and real,
to never invent numbers, and to avoid the tells that make cold email obviously automated
("I noticed you're…", "loved your content"). If the research is too thin, it says so
rather than padding.

**Critic** (`google/gemini-3.7-flash` — deliberately a different company)
Reads the research *and* the sentence, and judges whether the sentence invented anything
or is just generic. A model checking its own work shares its own blind spots, which is
why this is a different family entirely.

If the critic rejects, the writer gets **one retry** with the specific complaint. Fail
twice and it falls back to a plain, safe line — better a boring opener than a confident
lie sent to a stranger.

The shape it aims for (illustrative — from the sample data in `.tmp/fixtures/`):

> *"Running a live cohort while still taking every discovery call yourself is the kind
> of bottleneck that only shows up once the programme starts working."*

The point is that it references something the person actually published, not a
template with their company name slotted in. Cost: roughly **$8.60 per 1,000 leads**
across all three models.

### 4. Your review — the dashboard (`dashboard.py`)

`python dashboard.py`, then open `http://127.0.0.1:8000`. It runs only on your machine.

Each lead shows its fit score, the opener, and an expandable **"Research this was based
on"** panel with the actual posts and website text — so you can check the opener against
its source rather than trusting it. Edit any opener (it re-labels as "edited by you"),
tick **Approve** on the ones you want, then push.

Leaving "Test mode" ticked keeps you entirely in sample data.

### 5. Send — Instantly (`push_to_instantly.py`)

Only approved leads go. It creates a campaign named `Demo - Apollo Coaches - <date time>`
with a 3-step sequence (day 0, day 3, day 7), automatically finds every healthy mailbox
in your workspace, caps sending at 20/day, and turns on stop-on-reply
and the unsubscribe header.

The campaign is created as a **draft**, and the code then reads it back from Instantly to
*confirm* it's a draft rather than assuming. There is no code path anywhere in this
project that calls Instantly's activate endpoint.

---

## How it guarantees nobody is contacted twice

This is the failure that burns a sending domain, so there are three independent layers:

1. **The ledger** (`.tmp/ledger.json`) — a permanent record of everyone ever pushed or
   rejected. New runs skip them before spending a single credit on research.
2. **`skip_if_in_workspace`** — Instantly itself refuses any lead that already exists
   anywhere in your workspace, *including campaigns this tool never created*.
3. **`pushed_to_campaign`** — pushed leads are locked in the dashboard and filtered out
   of any later push.

They cover different failures: local file loss, a second campaign, a re-run mid-review.

Two files, deliberately kept apart:
- `leads_run.json` — **only the current batch**, replaced every run
- `ledger.json` — **everyone, ever**, appended forever

Test-mode data lives in a completely separate `.tmp/mock/` folder and can never reach a
real campaign.

---

## Reply handling

Instantly does this natively — we just configure it. `stop_on_reply` means anyone who
replies is pulled out of the sequence immediately and won't get the day-3 or day-7 email.
Replies land in your Instantly inbox. This system never reads or answers them.

---

## What it costs

| | per lead | per 1,000 |
|---|---|---|
| Apollo enrichment | ~1 credit | ~1,000 credits |
| Apify research | ~$0.01–0.02 | ~$15 |
| AI (3 models) | ~$0.009 | ~$8.60 |

Search is free. Test mode is free.

---

## When something goes wrong

Everything logs in plain language. The lines worth knowing:

- `apollo.no_matches` — your filters match nobody; loosen them on the dashboard's Targeting screen
- `verify.dropped` — one lead failed research, with the reason
- `personalize.qualifier_dropped` — the AI judged them a bad fit, with its reasoning
- `personalize.critic_reject` — the opener was rejected and is being rewritten
- `agents.response_truncated` — a model ran out of room; raise `max_tokens` for that role
- `push.mailboxes_discovered` — how many healthy mailboxes it found
- `ledger.skipped_already_handled` — leads skipped because we've dealt with them before

A high `openers_fallback` count in the summary means the research is coming back thin —
worth investigating before emailing anyone.
