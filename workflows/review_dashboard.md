## Objective
Give the operator one screen that runs the whole pipeline, configures every part of it, reads
every lead and its personalized opener, and pushes only what he approves into a draft
Instantly campaign.

## Inputs
- `.tmp/leads_run.json` — the current batch (`.tmp/mock/` in test mode)
- `.tmp/ledger.json` — everyone ever handled
- `.tmp/runs.json`, `.tmp/agent_stats.json` — run history and last per-role model stats
- `settings.json` — every tunable; absent means the built-in defaults apply
- `tools/dashboard_ui.html` — the page itself

## Tools used
1. `tools/dashboard.py` — FastAPI app, binds to 127.0.0.1:8000 only
2. `tools/config.py` — reads and writes `settings.json` for every Configure screen

## How to use it
```
cd tools && python dashboard.py
```
then open http://127.0.0.1:8000

**Operate**
1. *Run console* — set how many leads, confirm the cost estimate, press **Run pipeline**.
   Leave the mode on **Test** until you've seen it work: test mode makes zero API calls
   and spends nothing. A live run asks you to confirm the spend first.
2. *Review queue* — for each lead read the opener, expand *"The research this came from"*
   to check it against its source, edit anything off, tick **Approve for outreach**.
3. *Campaign* — press **Push N approved to draft campaign**, then open Instantly, review,
   and click Launch yourself. **Nothing sends until you do.**

**Configure** (all of it saved to `settings.json`, applied from the next run)
4. *Agents* — model, token budget, temperature and full instructions for the qualifier,
   writer and critic; plus the writer's retry count and fallback line.
5. *Targeting* — Apollo job titles, seniority, geography, company size, email status and
   the keyword. **Count the pool** uses Apollo's free search endpoint.
6. *Research* — the three Apify actors, whether to pull posts, per-lead limits, the
   minimum-research threshold and how many leads run at once.

**Records**
7. *Contact ledger* — everyone ever pushed or rejected, searchable.

## Design decisions worth knowing
- **Local only.** Binds to 127.0.0.1, never 0.0.0.0 — it holds lead data, can trigger
  metered API calls, and has no authentication, so it must not be reachable from the
  network.
- **Settings, not constants.** Every tunable lives in `settings.json` via `config.py`.
  Precedence: `settings.json` → the matching `.env` variable → `config.DEFAULTS`. Once a
  value is saved from a Configure screen it owns that value, and editing `.env` afterwards
  will not override it.
- **Secrets are never here.** No endpoint reads, writes or returns `.env`. API keys reach
  the tools only through `require_env`.
- **Test and live data are fully separate.** The mode toggle switches which batch, ledger
  and run history you are looking at; test data lives in `.tmp/mock/` and can never be
  pushed to a real campaign. Settings are shared by both — they are configuration, not
  data.
- **Edits are saved on blur**, one write per edit rather than per keystroke, and the chip
  flips to "Edited by you" as soon as the text differs.
- **Already-pushed leads are locked** — the textarea is read-only, the approve checkbox is
  replaced by the campaign name, and the API rejects a PATCH on them with a 409.
- **Unticking research also skips the models.** With nothing researched there is nothing
  to judge or reference, so leads come back with the fallback line instead of paying three
  models to invent something.

## Edge cases & failures (update as we learn)
- Pushing with nothing approved returns a clear error rather than creating an empty
  campaign.
- A failed run surfaces its error in a banner on the Run console, not just the terminal,
  and is still written to the run history.
- Only one run at a time; starting a second while one is in flight returns 409. Saving
  settings mid-run also returns 409 — half a batch on old settings and half on new is
  worse than waiting.
- Reloading the page during a run picks the progress polling back up rather than showing
  an idle console.
- A malformed `settings.json` raises on read rather than silently reverting to defaults,
  which would mean running filters and copy you did not intend.
- **Port 8000 already in use** means an older `dashboard.py` is still running and serving
  stale code. Stop it before starting a new one.
