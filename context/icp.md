# ICP — Apollo Proxy Filters

> **Live values are in `settings.json`**, edited on the dashboard's Targeting screen.
> This file is the reasoning behind them — update it when you change them, so the
> *why* does not get lost in a JSON file.

Define your own ICP before running this — the filters below are a worked example for an
online-coach / course-creator audience, not a universal setting. Record your real ICP
definition wherever your team keeps positioning material, and treat this file as the
translation of it into what Apollo can actually query.

**The core problem this file exists to solve:** Apollo.io has no fields for things like
program revenue, audience size, or ad budget. It filters on firmographic data — job
title, industry, company size, location. So any Apollo query is a **best-effort proxy**,
not a precise match. Verification (`verify_apify.py`) is where ICP fit is actually
confirmed, using LinkedIn bio signals and live website/social checks. Treat everything
Apollo returns as an unverified candidate, not a qualified lead.

## Apollo Search Filters (example set — tune based on verify-step drop rates)

- **Job titles**: `Founder`, `CEO`, `Owner`, `Coach`, `Course Creator`, `Online Coach`
- **Industry** (Apollo taxonomy): `Professional Training & Coaching`, `E-Learning`,
  `Health, Wellness & Fitness` (secondary)
- **Company size**: 1–10 employees (solo operators / small teams)
- **Geography**: United States, United Kingdom, Australia, Canada
- **Keywords**: note that Apollo's `q_keywords` is a **phrase match, not a bag of
  words** — multi-word values narrow the pool far more aggressively than you expect.
  Use the dashboard's free "Count the pool" button before committing to a value.

## Verification-Step Fit Signals (checked in verify_apify.py, not Apollo)

A candidate is treated as ICP-fit if the LinkedIn bio or company site shows:
- A paid program, course, or mentorship offer (not just consulting/services)
- Active social presence (a business page linked or findable)
- Signals of scale (testimonials, cohort language, "students", "clients") rather than a
  brand-new solo page with no offer live yet

## Tuning Log

Update this section as real runs come back with drop-rate data from `verify_apify.py`.
If a filter combination is producing a high false-positive rate (Apollo returns lots of
candidates that fail verification), narrow it here before the next run.

- *(no runs logged — this is the v1 starting filter set)*
