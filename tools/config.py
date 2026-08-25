"""Editable settings, shared by every tool and owned by the dashboard.

WHY THIS EXISTS
---------------
Every tunable in this project used to be a constant at the top of a Python file —
`ICP_FILTERS`, `SEQUENCE_STEPS`, `DAILY_SEND_CAP`, the three model roles. That is fine
for a script and useless for a dashboard: you cannot change a Python constant from a
browser. This module moves those tunables into one JSON file that both the tools and the
dashboard read.

WHERE VALUES COME FROM (first match wins)
-----------------------------------------
  1. `settings.json` at the repo root — written by the dashboard, editable by hand
  2. the matching environment variable, where one exists (see `.env.example`)
  3. the DEFAULTS below, which are exactly the constants the tools used before

So a fresh checkout with no `settings.json` behaves identically to the old code. Once you
change something in the dashboard, that section of `settings.json` exists and owns those
values — editing `.env` afterwards will not override a setting you have already set in
the UI. That is deliberate: two places silently competing to define one value is worse
than one place clearly winning.

NEVER PUT SECRETS HERE. API keys stay in `.env` and are read through
`_common.require_env`. `settings.json` holds only non-secret configuration, so it is safe
to read, diff and hand-edit.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from _common import REPO_ROOT, _file_lock, _write_atomic, get_logger

log = get_logger(__name__)

SETTINGS_PATH = REPO_ROOT / "settings.json"

# Sections a caller may read or write. Anything else is rejected, so a typo in an API
# call cannot quietly create a section no tool reads.
SECTIONS = ("targeting", "research", "agents", "campaign")


# ── Defaults ───────────────────────────────────────────────────────────────────────────
# These are the previous in-code constants, moved verbatim. Changing a value here changes
# the default for anyone who has not overridden it in settings.json.

DEFAULTS: dict[str, Any] = {
    # Apollo proxy filters for the coach/course-creator ICP. See context/icp.md for why
    # these are proxies rather than an exact match.
    #
    # CAREFUL WITH keywords: Apollo treats it as a phrase, not a bag of words. Measured
    # against the live API 2026-08-20 with all other filters applied:
    #     (none) 883,985 · "coach" 6,113 · "coaching" 3,297 · "online coaching" 16
    #     · "coaching course program mentorship" 0
    # Multi-word values silently return zero results, which looks exactly like "no leads
    # today" rather than a bug.
    "targeting": {
        "titles": ["Founder", "CEO", "Owner", "Coach", "Course Creator"],
        "seniorities": ["owner", "founder", "c_suite"],
        "employee_ranges": ["1,10"],
        "locations": ["United States", "United Kingdom", "Australia", "Canada"],
        "email_status": ["verified"],
        "keywords": "coaching",
    },
    "research": {
        # Actor IDs are public identifiers, not secrets. Defaults verified on
        # apify.com/store 2026-08-20, pay-per-event.
        "linkedin_actor": "harvestapi/linkedin-profile-scraper",
        "posts_actor": "harvestapi/linkedin-profile-posts",
        "company_actor": "apify/website-content-crawler",
        # Recent posts are what make an opener sound like a human actually looked.
        # Turning this off is cheaper and noticeably worse.
        "posts_enabled": True,
        "max_posts": 5,
        "site_pages": 3,
        # Below this much research text there is nothing to personalize from and nothing
        # for the qualifier to reason over.
        "min_research_chars": 80,
        # Unbounded concurrency exhausts the httpx pool and triggers rate-limit storms
        # against metered APIs. Five at a time is plenty.
        "concurrency": 5,
    },
    "agents": {
        "qualifier": {
            "env_var": "OPENROUTER_QUALIFIER_MODEL",
            "model": "openai/gpt-5.6-luna",
            # Generous, because reasoning models spend most of their completion budget on
            # thinking tokens before emitting a single character of JSON. Too low and the
            # JSON arrives truncated, which reads as "model unavailable".
            "max_tokens": 1500,
            "temperature": None,  # null = don't send the parameter at all
            "system": (
                "You assess whether a person is a good fit for an agency that helps "
                "online coaches and course creators run paid social ads.\n\n"
                "A GOOD fit sells a paid coaching program, course, mastermind, or "
                "mentorship to individuals and has an audience or offer already live.\n"
                "A BAD fit is: a corporate employee, a B2B software company, a agency "
                "serving other businesses, a life coach with no visible paid offer, a "
                "student, or someone whose work is unrelated.\n\n"
                "Judge ONLY from the research given. If the research is too thin to "
                "tell, say so rather than guessing.\n\n"
                'Reply with JSON only:\n{"fit": true|false, "score": 0-100, "reason": '
                '"one short sentence"}'
            ),
            # A lead the qualifier scores below this is dropped even if it said "fit".
            "min_score": 50,
        },
        "writer": {
            "env_var": "OPENROUTER_WRITER_MODEL",
            "model": "anthropic/claude-sonnet-5",
            "max_tokens": 150,
            "temperature": None,
            "system": (
                "You write the opening line of a cold outreach email to an online coach "
                "or course creator.\n\nRules:\n"
                "- Output EXACTLY one sentence. No greeting, no sign-off, no quotes, no "
                "preamble.\n- Under 28 words.\n"
                "- Reference something SPECIFIC and REAL from the research — ideally "
                "something they recently posted about. Generic flattery is worthless and "
                "obvious.\n"
                "- Use ONLY facts stated in the research. Never invent revenue, follower "
                "counts, client numbers, results, or timelines.\n"
                "- Sound like a person who actually looked at their work for thirty "
                'seconds. Do NOT use: "impressive", "loved your", "I noticed you\'re", '
                '"reaching out because", "came across".\n'
                "- If the research is too thin to say anything genuinely specific, "
                "output exactly: INSUFFICIENT"
            ),
            "max_opener_words": 28,
            # Attempts after the first. 1 = writer gets one rewrite with the critic's
            # complaint before falling back.
            "retries": 1,
            "fallback_opener": "I came across {company} and wanted to reach out directly.",
        },
        "critic": {
            "env_var": "OPENROUTER_CRITIC_MODEL",
            # Deliberately a different vendor from the writer: a critic sharing the
            # writer's family shares its blind spots.
            "model": "google/gemini-3.7-flash",
            "max_tokens": 1500,
            "temperature": 0.0,  # supported here; pin the judge deterministic
            "system": (
                "You review one opening sentence from a cold email against the research "
                "it was supposedly based on.\n\nFail the sentence if ANY of these are "
                "true:\n"
                "- It states a fact not supported by the research (invented numbers, "
                "results, or claims).\n"
                "- It is generic enough that it could be sent to any coach — no real "
                "specificity.\n"
                '- It reads like AI filler ("impressive work", "loved your content", "I '
                "noticed you're\").\n"
                "- It is longer than one sentence, or over 28 words.\n\n"
                "Pass it if it is specific, grounded in the research, and sounds like a "
                "human wrote it.\n\n"
                'Reply with JSON only:\n{"pass": true|false, "reason": "one short '
                'sentence"}'
            ),
        },
    },
    "campaign": {
        "name_prefix": "Demo - Apollo Coaches",
        "daily_limit": 20,
        # Instantly accepts only a fixed enum of timezones and rejects the campaign with
        # a 400 otherwise. "America/New_York" is NOT in it — America/Detroit is the
        # Eastern-time entry that is. Verified against the live API 2026-08-20.
        "timezone": "America/Detroit",
        "send_from": "09:00",
        "send_to": "17:00",
        "stop_on_reply": True,
        "insert_unsubscribe_header": True,
        # Bodies are HTML: Instantly's API expects <br/> for line breaks.
        "steps": [
            {
                "delay": 0,
                "subject": "quick one for {{firstName}}",
                "body": (
                    "Hi {{firstName}},<br/><br/>{{personalization}}<br/><br/>"
                    "[One or two lines on who you are and the specific outcome you "
                    "deliver. Replace this before your first real send — placeholder "
                    "copy is the fastest way to burn a good lead.]<br/><br/>"
                    "Worth a quick look at what that could mean for {{companyName}}?"
                    "<br/><br/>Reply STOP at any time to opt out."
                ),
            },
            {
                "delay": 3,
                "subject": "re: quick one for {{firstName}}",
                "body": (
                    "Hi {{firstName}} — following up in case this got buried.<br/><br/>"
                    "[Offer one concrete, low-friction next step — an example, a "
                    "teardown, a relevant result. No pressure either way.]<br/><br/>"
                    "Reply STOP at any time to opt out."
                ),
            },
            {
                "delay": 7,
                "subject": "last one from me",
                "body": (
                    "Hi {{firstName}},<br/><br/>I'll leave it here — if scaling paid "
                    "acquisition becomes a priority later, feel free to reach out any "
                    "time.<br/><br/>Reply STOP at any time to opt out."
                ),
            },
        ],
    },
}


# ── Reading ────────────────────────────────────────────────────────────────────────────


def _load_file() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        raw = json.loads(SETTINGS_PATH.read_text())
    except json.JSONDecodeError as exc:
        # Fail loudly. Silently falling back to defaults would mean a run using filters
        # and sequence copy you did not intend, with no visible sign anything was wrong.
        raise RuntimeError(
            f"settings.json is not valid JSON ({exc}). Fix or delete it — deleting it "
            "restores the built-in defaults."
        ) from None
    return raw if isinstance(raw, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Overlay `override` on `base`, recursing into nested dicts.

    Lists are replaced wholesale, never merged element-wise — a saved list of sequence
    steps or job titles is the complete intended list, not an addition to the defaults.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def section(name: str) -> dict[str, Any]:
    """One settings section, with saved values layered over the defaults."""
    if name not in SECTIONS:
        raise KeyError(f"Unknown settings section {name!r}. Known: {', '.join(SECTIONS)}")
    return _deep_merge(DEFAULTS[name], _load_file().get(name) or {})


def all_settings() -> dict[str, Any]:
    """Every section, merged. What the dashboard renders its config screens from."""
    return {name: section(name) for name in SECTIONS}


def saved(name: str) -> dict[str, Any]:
    """Only what settings.json explicitly holds for a section — defaults NOT merged.

    Callers use this to tell "the user chose this value" apart from "this is the
    built-in default", which is what makes the .env fallback in `with_env` possible.
    """
    return (_load_file().get(name) or {}) if name in SECTIONS else {}


def with_env(name: str, key: str, env_var: str, allow_empty: bool = False) -> Any:
    """One setting, resolved by the module's precedence rule.

    settings.json (only if explicitly saved) → the environment variable → the default.

    `allow_empty` decides what an env var set to "" means. For almost everything, blank
    is an absence and the default should win — a blank actor ID is a broken run, not a
    choice. Pass allow_empty=True only where blank is a real instruction, which is how
    `APIFY_POSTS_ACTOR=` has always switched post scraping off.
    """
    explicit = saved(name)
    if key in explicit and explicit[key] not in (None, ""):
        return explicit[key]
    value = os.environ.get(env_var)
    if value or (value is not None and allow_empty):
        return value
    return section(name)[key]


def model_for(role: str) -> str:
    """Resolve a role's model slug: settings.json, then .env, then the built-in default."""
    explicit = (saved("agents").get(role) or {})
    if explicit.get("model"):
        return str(explicit["model"])
    defaults = DEFAULTS["agents"][role]
    return os.environ.get(defaults["env_var"]) or str(defaults["model"])


# ── Writing ────────────────────────────────────────────────────────────────────────────


def save_section(name: str, values: dict[str, Any]) -> dict[str, Any]:
    """Persist one section and return it as the tools will now see it.

    Locked and atomic for the same reason the ledger is: the dashboard and a CLI run are
    separate processes, and a torn write here means a run using half-old settings.
    """
    if name not in SECTIONS:
        raise KeyError(f"Unknown settings section {name!r}. Known: {', '.join(SECTIONS)}")
    if not isinstance(values, dict):
        raise ValueError(f"Settings section {name!r} must be an object, got {type(values).__name__}")

    with _file_lock(SETTINGS_PATH):
        current = _load_file()
        current[name] = _deep_merge(current.get(name) or {}, values)
        _write_atomic(SETTINGS_PATH, json.dumps(current, indent=2) + "\n")

    log.info("config.saved", section=name, keys=sorted(values.keys()))
    return section(name)


def reset_section(name: str) -> dict[str, Any]:
    """Forget everything saved for one section, restoring the built-in defaults."""
    if name not in SECTIONS:
        raise KeyError(f"Unknown settings section {name!r}. Known: {', '.join(SECTIONS)}")
    with _file_lock(SETTINGS_PATH):
        current = _load_file()
        current.pop(name, None)
        _write_atomic(SETTINGS_PATH, json.dumps(current, indent=2) + "\n")
    log.info("config.reset", section=name)
    return section(name)
