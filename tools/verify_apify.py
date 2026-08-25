"""Verify + deep-research Apollo candidates via Apify actors. Drop bad fits.

See workflows/verify_enrich.md for the full spec.

Per lead this runs up to three Apify actors:
  1. LinkedIn profile (+ email search)  — is this a real person, what do they do, and
     find their email if Apollo didn't supply one
  2. LinkedIn recent posts              — what are they actually talking about right now
  3. Company website crawl              — what do they actually sell

Actor IDs default in code and can be overridden in .env. Input AND output schemas below
were confirmed empirically against live runs on 2026-08-20 — note the two LinkedIn
actors take DIFFERENT input keys despite being from the same publisher:
  harvestapi/linkedin-profile-scraper : {"queries": [url], "profileScraperMode": ...}
  harvestapi/linkedin-profile-posts   : {"targetUrls": [url], "maxPosts": N}
  apify/website-content-crawler       : {"startUrls": [{"url": ...}], "maxCrawlPages": N}

See context/api_notes.md for the full list of traps these actors set.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

import config
from _common import (
    RUN_FILE,
    Lead,
    gather_limited,
    get_logger,
    ledger_filter,
    ledger_record,
    load_fixture,
    load_tmp,
    require_env,
    request_json,
    save_tmp,
    valid_email,
)

log = get_logger(__name__)

APIFY_RUN_SYNC_URL = "https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"


class ApifyActorError(RuntimeError):
    """An actor rejected our input or failed. Carries Apify's own explanation."""


# website-content-crawler defaults to 8GB/run, which exhausts a 16GB Apify account after
# two concurrent leads. 2GB is ample for crawling three pages of text.
COMPANY_ACTOR_MEMORY_MB = 2048


def research_config() -> dict[str, Any]:
    """Everything this stage needs, resolved from settings.json / .env / defaults.

    Actor IDs are public identifiers, not secrets, so they live in settings alongside the
    limits. Only APIFY_TOKEN comes from .env.

    Posts are skipped when the Research screen switches them off OR when the actor
    resolves to empty — `APIFY_POSTS_ACTOR=` in .env is the long-standing way to turn
    them off and still works.
    """
    settings = config.section("research")
    posts_actor = config.with_env(
        "research", "posts_actor", "APIFY_POSTS_ACTOR", allow_empty=True
    )
    return {
        "linkedin_actor": config.with_env("research", "linkedin_actor", "APIFY_LINKEDIN_ACTOR"),
        "company_actor": config.with_env("research", "company_actor", "APIFY_COMPANY_ACTOR"),
        "posts_actor": posts_actor if settings.get("posts_enabled", True) else "",
        "max_posts": int(settings["max_posts"]),
        "site_pages": int(settings["site_pages"]),
        "min_research_chars": int(settings["min_research_chars"]),
        "concurrency": int(settings["concurrency"]),
    }


def _first_nonempty(source: dict, *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value:
            return str(value)
    return ""


def _best_email(profile: dict) -> tuple[str | None, str]:
    """Pick the best discovered email from the actor's `emails` list.

    Shape (confirmed against a live run):
        [{"email": "...", "status": "risky", "qualityScore": 60,
          "validEmailServer": true, "catchAllDomain": true, "free": false}]

    Sorted by qualityScore so the most deliverable wins. Anything the actor flags as
    invalid is skipped — sending to a bad address costs domain reputation.
    """
    candidates = profile.get("emails")
    if not isinstance(candidates, list):
        return None, ""

    ranked = sorted(
        (c for c in candidates if isinstance(c, dict) and valid_email(c.get("email"))),
        key=lambda c: int(c.get("qualityScore") or 0),
        reverse=True,
    )
    for candidate in ranked:
        if str(candidate.get("status", "")).lower() == "invalid":
            continue
        if candidate.get("validEmailServer") is False:
            continue
        return candidate["email"], f"{candidate.get('status')}/{candidate.get('qualityScore')}"
    return None, ""


async def _run_actor(
    client: httpx.AsyncClient,
    token: str,
    actor_id: str,
    run_input: dict,
    memory_mbytes: int | None = None,
) -> list[dict]:
    """Run an Apify actor synchronously and return its dataset items.

    The token goes in the Authorization header, NOT the query string: httpx puts the
    full URL into exception messages, and a token in the query string ends up copied
    into logs verbatim.

    `memory_mbytes` matters more than it looks. Apify caps total memory across all your
    concurrent runs (16GB on the default plan). website-content-crawler defaults to 8GB
    per run, so just two concurrent leads exhaust the account and the rest fail with a
    402. Capping it lets many leads run side by side.
    """
    url = APIFY_RUN_SYNC_URL.format(actor_id=actor_id.replace("/", "~"))
    params = {"memory": memory_mbytes} if memory_mbytes else None
    try:
        result = await request_json(
            client, "POST", url,
            headers={"Authorization": f"Bearer {token}"},
            params=params, json=run_input, timeout=180,
        )
    except httpx.HTTPStatusError as exc:
        # Apify explains rejected input in the body; without it a 400 is undiagnosable.
        detail = ""
        try:
            detail = str(exc.response.json().get("error", {}).get("message", ""))[:300]
        except Exception:
            detail = exc.response.text[:300]
        raise ApifyActorError(f"{actor_id} returned {exc.response.status_code}: {detail}") from None
    return result if isinstance(result, list) else []


async def _verify_one(
    client: httpx.AsyncClient, cfg: dict[str, Any], lead: Lead
) -> Lead:
    """Research one lead. Sets verified True/False and fills the research fields."""
    if not lead.linkedin_url:
        lead.verified = False
        lead.verification_notes = "no_linkedin_url_to_verify"
        return lead

    # --- 1. Profile (+ email discovery when Apollo gave us nothing usable) -------------
    need_email = not valid_email(lead.email)
    mode = (
        "Profile details + email search ($10 per 1k)"
        if need_email
        else "Profile details no email ($4 per 1k)"
    )
    try:
        profiles = await _run_actor(
            client,
            cfg["APIFY_TOKEN"],
            cfg["linkedin_actor"],
            {"queries": [lead.linkedin_url], "profileScraperMode": mode},
        )
    except (httpx.HTTPError, ApifyActorError) as exc:
        lead.verified = False
        lead.verification_notes = f"linkedin_actor_failed: {exc}"
        return lead

    if not profiles:
        lead.verified = False
        lead.verification_notes = "linkedin_profile_unreachable"
        return lead

    profile = profiles[0]

    # On failure this actor returns HTTP 200 with a per-item error envelope
    # ({"element": null, "status": 404, "error": "Profile not found"}) rather than an
    # HTTP error — so a missing profile looks like a successful empty result unless we
    # check for it explicitly.
    if profile.get("error") or (profile.get("element") is None and profile.get("status")):
        lead.verified = False
        lead.verification_notes = (
            f"linkedin profile not retrievable: {profile.get('error') or profile.get('status')}"
        )
        return lead

    lead.linkedin_bio_snippet = _first_nonempty(profile, "about", "summary", "headline")[:600]
    lead.title = lead.title or _first_nonempty(profile, "headline", "occupation")

    # Audience size is a genuine ICP signal (the real ICP wants people with an existing
    # audience), and it gives the qualifier something concrete to reason about.
    followers = profile.get("followerCount")
    if isinstance(followers, int) and followers > 0:
        lead.research_notes = f"linkedin_followers={followers}"

    if need_email:
        found, quality = _best_email(profile)
        if found:
            lead.email = found
            lead.email_missing = False
            log.info("verify.email_discovered", lead=lead.display_name(), quality=quality)

    if not valid_email(lead.email):
        lead.verified = False
        lead.verification_notes = "no_valid_email_found"
        return lead

    # --- 2 & 3. Posts and website, concurrently ----------------------------------------
    # These are independent of each other and each Apify actor run can take 60s+, so
    # running them in parallel roughly halves the wall-clock time per lead.
    async def _fetch_posts() -> None:
        if not cfg.get("posts_actor"):
            return
        try:
            # This actor takes `targetUrls`, NOT `queries` (unlike the profile actor).
            # Do NOT add `postedLimit` — measured 2026-08-20, passing "6months" returned
            # zero posts for a profile that returns 5 without it.
            posts = await _run_actor(
                client, cfg["APIFY_TOKEN"], cfg["posts_actor"],
                {"targetUrls": [lead.linkedin_url], "maxPosts": cfg["max_posts"]},
            )
            snippets = [
                _first_nonempty(post, "content", "text", "postContent")[:280]
                for post in posts[: cfg["max_posts"]]
            ]
            lead.recent_activity = "\n---\n".join(s for s in snippets if s)[:1500]
        except (httpx.HTTPError, ApifyActorError) as exc:
            log.warning("verify.posts_actor_failed", lead=lead.display_name(), error=str(exc))

    async def _fetch_site() -> None:
        if not lead.company_domain:
            return
        try:
            pages = await _run_actor(
                client, cfg["APIFY_TOKEN"], cfg["company_actor"],
                {
                    "startUrls": [{"url": f"https://{lead.company_domain}"}],
                    "maxCrawlPages": cfg["site_pages"],
                    "crawlerType": "cheerio",  # no browser needed for text; far lighter
                },
                memory_mbytes=COMPANY_ACTOR_MEMORY_MB,
            )
            texts = [
                _first_nonempty(page, "text", "markdown", "description")
                for page in pages[: cfg["site_pages"]]
            ]
            lead.company_summary = "\n".join(t for t in texts if t)[:2000]
        except (httpx.HTTPError, ApifyActorError) as exc:
            log.warning("verify.company_actor_failed", lead=lead.display_name(), error=str(exc))

    await asyncio.gather(_fetch_posts(), _fetch_site())

    # --- 4. Did we actually learn anything? --------------------------------------------
    # ICP fit is NOT judged here any more — the qualifier model in personalize_copy.py
    # does that properly, with real judgement instead of keyword matching. This stage
    # only rejects leads we know nothing about, since there's nothing to personalize
    # from and nothing for the qualifier to reason over.
    research_chars = len(lead.linkedin_bio_snippet) + len(lead.company_summary) + len(lead.recent_activity)
    if research_chars < cfg["min_research_chars"]:
        lead.verified = False
        lead.verification_notes = f"almost no research found ({research_chars} chars)"
        return lead

    lead.verified = True
    lead.verification_notes = "ok"
    lead.research_notes = " ".join(
        filter(None, [
            lead.research_notes,
            f"bio:{len(lead.linkedin_bio_snippet)}c",
            f"posts:{len(lead.recent_activity)}c",
            f"site:{len(lead.company_summary)}c",
        ])
    )
    return lead


def _mock_verify(lead: Lead) -> Lead:
    lead.verified = valid_email(lead.email)
    lead.verification_notes = "mock_ok" if lead.verified else "no_valid_email_found"
    lead.linkedin_bio_snippet = "Runs a 12-week coaching cohort for course creators."
    lead.company_summary = "Online coaching business selling a group program. Mock data."
    lead.recent_activity = "Posted about launching a new cohort next month. Mock data."
    lead.research_notes = "mock"
    return lead


async def verify(candidates: list[Lead], mock: bool) -> list[Lead]:
    if mock:
        results = [_mock_verify(lead) for lead in candidates]
    else:
        # Only the token is a secret that must come from .env. Actor IDs are public
        # identifiers with working defaults — override any of them in .env if you want
        # a different actor, or set APIFY_POSTS_ACTOR empty to skip post scraping.
        cfg: dict[str, Any] = {**require_env("APIFY_TOKEN"), **research_config()}

        async with httpx.AsyncClient(timeout=200) as client:
            results = await gather_limited(
                candidates,
                lambda lead: _verify_one(client, cfg, lead),
                concurrency=cfg["concurrency"],
            )

    verified = [lead for lead in results if lead.verified]
    dropped = [lead for lead in results if not lead.verified]

    for lead in dropped:
        log.info("verify.dropped", lead=lead.display_name(), reason=lead.verification_notes)
    if dropped:
        # Remember rejects so we never pay to research the same bad fit twice.
        ledger_record(dropped, outcome="rejected_at_verify", mock=mock)

    if results:
        drop_rate = len(dropped) / len(results)
        if drop_rate > 0.5:
            log.warning(
                "verify.high_drop_rate",
                drop_rate=round(drop_rate, 2),
                hint="tune the filters on the dashboard's Targeting screen",
            )

    log.info("verify.done", verified=len(verified), dropped=len(dropped))
    return verified


async def main(mock: bool = False, limit: int | None = None) -> list[Lead]:
    candidates = load_tmp("apollo_raw.json", mock=mock)
    candidates = ledger_filter(candidates, mock=mock)
    if limit is not None:
        candidates = candidates[:limit]

    verified = await verify(candidates, mock=mock)
    save_tmp(RUN_FILE, verified, mock=mock)
    return verified


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(mock=args.mock, limit=args.limit))
