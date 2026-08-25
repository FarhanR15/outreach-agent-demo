"""Find candidate leads in Apollo.io, then enrich them to get real contact details.

See workflows/scrape_apollo.md for the full spec. Filters come from context/icp.md.

TWO STAGES, because Apollo splits them (confirmed 2026-08-20 against docs.apollo.io):

  1. SEARCH  — POST /api/v1/mixed_people/api_search   [0 credits, free]
     Returns ONLY: id, first_name, last_name_obfuscated ("Do***e"), title, has_email,
     organization.name. Deliberately NO email, NO real last name, NO linkedin_url,
     NO company domain. Quote from the docs: "This endpoint doesn't return email
     addresses or phone numbers."

  2. ENRICH — POST /api/v1/people/bulk_match          [~1 credit per person, MAX 10/call]
     Takes the ids from step 1 and returns the real email, last_name, linkedin_url and
     organization.primary_domain that the rest of the pipeline depends on.

COST: search is free; enrichment burns roughly 1 Apollo credit per lead (more if
personal phone reveal is enabled — it is NOT, deliberately). We pre-filter on
`has_email` so credits are never spent on records that can't yield an email anyway.

Placeholder-email trap: Apollo's /people/{id} endpoint returns the literal string
"email_not_unlocked@domain.com" rather than null for locked emails, which passes naive
truthiness checks. We never use that endpoint, and `valid_email` guards the literal
anyway.
"""

from __future__ import annotations

import argparse
import asyncio

import httpx

import config
from _common import (
    Lead,
    get_logger,
    ledger_filter,
    ledger_index_apollo_ids,
    ledger_known_apollo_ids,
    load_fixture,
    require_env,
    request_json,
    save_tmp,
    valid_email,
)

log = get_logger(__name__)

APOLLO_BASE = "https://api.apollo.io/api/v1"
SEARCH_URL = f"{APOLLO_BASE}/mixed_people/api_search"
BULK_MATCH_URL = f"{APOLLO_BASE}/people/bulk_match"

BULK_MATCH_CHUNK = 10  # hard API limit
MAX_PER_PAGE = 100
MAX_PAGE = 500


def icp_filters() -> dict[str, list[str] | str]:
    """Build Apollo's search parameters from the saved targeting settings.

    The settings use plain names ("titles"); Apollo wants its own bracketed parameter
    names. That translation lives here rather than in the settings file so the dashboard
    never has to know Apollo's parameter spelling.

    CAREFUL WITH q_keywords: it is effectively a phrase match, not a bag of words. See
    the measured counts in config.DEFAULTS["targeting"] — multi-word values silently
    return zero results, which looks exactly like "no leads today" rather than a bug.
    """
    targeting = config.section("targeting")
    return {
        "person_titles[]": list(targeting["titles"]),
        "person_seniorities[]": list(targeting["seniorities"]),
        "organization_num_employees_ranges[]": list(targeting["employee_ranges"]),
        "person_locations[]": list(targeting["locations"]),
        "contact_email_status[]": list(targeting["email_status"]),
        "q_keywords": str(targeting["keywords"]),
    }


def _to_lead(person: dict) -> Lead:
    """Build a Lead from a bulk_match (enrichment) record, not a search record."""
    org = person.get("organization") or {}
    email = person.get("email")
    return Lead(
        apollo_id=person.get("id") or "",
        email=email if valid_email(email) else None,
        email_missing=not valid_email(email),
        first_name=person.get("first_name") or "",
        last_name=person.get("last_name") or "",
        title=person.get("title") or person.get("headline") or "",
        company_name=org.get("name") or "",
        company_domain=org.get("primary_domain") or "",
        linkedin_url=person.get("linkedin_url") or "",
        source="apollo",
    )


async def _search_page(client: httpx.AsyncClient, api_key: str, page: int, per_page: int) -> list[dict]:
    """One page of the free search endpoint. Params go in the QUERY STRING, not a body."""
    params: dict = {**icp_filters(), "page": page, "per_page": per_page}
    data = await request_json(
        client, "POST", SEARCH_URL, params=params, headers={"x-api-key": api_key}
    )
    total = data.get("total_entries", 0)
    if page == 1:
        log.info("apollo.search_pool", total_entries=total)
        if total == 0:
            keywords = str(icp_filters().get("q_keywords", ""))
            log.warning(
                "apollo.no_matches",
                hint=(
                    f"q_keywords={keywords!r} is {len(keywords.split())} words — Apollo "
                    "treats it as a phrase, so more than one word usually matches nothing"
                    if len(keywords.split()) > 1
                    else "the filter combination on the Targeting screen matches nobody; loosen it"
                ),
            )
    return data.get("people", []) or []


async def estimate_pool() -> dict[str, int | str]:
    """How many people match the current targeting. Uses the FREE search endpoint.

    One page-1 request with per_page=1: Apollo returns `total_entries` for the whole
    result set regardless of page size, so this costs nothing and transfers almost
    nothing. Used by the dashboard so filters can be tuned before any credit is spent.
    """
    cfg = require_env("APOLLO_API_KEY")
    async with httpx.AsyncClient(timeout=45) as client:
        data = await request_json(
            client, "POST", SEARCH_URL,
            params={**icp_filters(), "page": 1, "per_page": 1},
            headers={"x-api-key": cfg["APOLLO_API_KEY"]},
        )
    total = int(data.get("total_entries") or 0)
    known = len(ledger_known_apollo_ids())
    return {"total": total, "already_handled": known, "keywords": str(icp_filters()["q_keywords"])}


async def _bulk_enrich(client: httpx.AsyncClient, api_key: str, person_ids: list[str]) -> list[dict]:
    """Enrich up to 10 people per call. This is the step that costs credits."""
    data = await request_json(
        client,
        "POST",
        BULK_MATCH_URL,
        params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"details": [{"id": pid} for pid in person_ids]},
        timeout=60,
    )
    log.info(
        "apollo.enriched_chunk",
        requested=len(person_ids),
        credits_consumed=data.get("credits_consumed"),
    )
    return data.get("matches", []) or []


async def scrape(limit: int, mock: bool) -> list[Lead]:
    if mock:
        leads = load_fixture("apollo_raw.json")[:limit]
        log.info("apollo.mock", count=len(leads))
        return leads

    cfg = require_env("APOLLO_API_KEY")
    api_key = cfg["APOLLO_API_KEY"]

    # People we've already dealt with, keyed by Apollo id. Filtering on these during the
    # FREE search is what stops us paying a credit to enrich someone we then discard —
    # and stops the pipeline stalling once page 1 is entirely known.
    known_ids = ledger_known_apollo_ids(mock=mock)

    async with httpx.AsyncClient(timeout=45) as client:
        # --- Stage 1: free search, collect ids of NEW people who have an email --------
        candidate_ids: list[str] = []
        page = 1
        skipped_known = 0
        while len(candidate_ids) < limit and page <= MAX_PAGE:
            people = await _search_page(client, api_key, page, MAX_PER_PAGE)
            if not people:
                break
            for person in people:
                if len(candidate_ids) >= limit:
                    break
                pid = person.get("id")
                if not pid or not person.get("has_email"):
                    continue
                if pid in known_ids:
                    skipped_known += 1
                    continue
                candidate_ids.append(pid)
            page += 1

        log.info(
            "apollo.search_done",
            new_candidates=len(candidate_ids),
            skipped_already_handled=skipped_known,
            pages_scanned=page - 1,
        )
        if not candidate_ids:
            return []

        # --- Stage 2: paid enrichment, in chunks of 10 --------------------------------
        matches: list[dict] = []
        for start in range(0, len(candidate_ids), BULK_MATCH_CHUNK):
            chunk = candidate_ids[start : start + BULK_MATCH_CHUNK]
            try:
                matches.extend(await _bulk_enrich(client, api_key, chunk))
            except httpx.HTTPError as exc:
                log.warning("apollo.enrich_chunk_failed", start=start, error=str(exc))

    leads = [_to_lead(person) for person in matches]
    usable = [lead for lead in leads if lead.email]
    if len(usable) < len(leads):
        log.info("apollo.dropped_no_email_after_enrich", count=len(leads) - len(usable))

    log.info("apollo.done", count=len(usable))
    return usable


async def main(limit: int = 5, mock: bool = False) -> list[Lead]:
    leads = await scrape(limit=limit, mock=mock)

    kept = ledger_filter(leads, mock=mock)

    # Self-healing index. A lead can reach here and still be dropped — because the
    # ledger knew them by email but not by Apollo id (an entry written before ids were
    # recorded, or a person Apollo returns under a different id). Record their id now so
    # the NEXT run skips them during the free search instead of paying to enrich them
    # again. Without this the same people are re-enriched on every run, forever.
    kept_keys = {lead.dedupe_key() for lead in kept}
    dropped = [lead for lead in leads if lead.dedupe_key() not in kept_keys]
    if dropped:
        added = ledger_index_apollo_ids(dropped, mock=mock)
        if added:
            log.info("apollo.backfilled_ids", count=added)

    save_tmp("apollo_raw.json", kept, mock=mock)
    return kept


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, mock=args.mock))
