"""Orchestrate scrape -> verify -> personalize, then stop for human review.

See workflows/run_pipeline.md for the full spec.

By design this does NOT push to Instantly. Research and drafting are automated; deciding
who actually gets emailed is not. Review the batch in the dashboard (`python
dashboard.py`), approve the leads you want, and push from there — or run
`python push_to_instantly.py` deliberately once you've reviewed .tmp/leads_run.json.
"""

from __future__ import annotations

import argparse
import asyncio

import personalize_copy
import scrape_apollo
import verify_apify
from _common import get_logger, require_env

log = get_logger(__name__)


async def main(limit: int = 5, mock: bool = False) -> None:
    if not mock:
        # Fail before spending anything if the environment is incomplete. Only real
        # secrets are listed: actor IDs and model slugs have working code defaults
        # (see verify_apify.py and agents.py), so requiring them here would be wrong.
        require_env("APOLLO_API_KEY", "APIFY_TOKEN", "OPENROUTER_API_KEY")

    scraped = await scrape_apollo.main(limit=limit, mock=mock)
    if not scraped:
        log.error(
            "run_pipeline.stopped",
            stage="scrape",
            reason="Apollo returned no new leads (already-handled leads are skipped)",
        )
        return

    verified = await verify_apify.main(mock=mock, limit=limit)
    if not verified:
        log.error(
            "run_pipeline.stopped",
            stage="verify",
            reason="no leads survived verification — see per-lead reasons above, and "
            "consider loosening the filters in context/icp.md",
        )
        return

    personalized = await personalize_copy.main(mock=mock)

    # Count fallbacks directly: an opener that passed the critic on its second attempt
    # ("llm_retry") is a good opener, not a fallback, and reporting it as one would
    # overstate how often personalization failed.
    fallbacks = sum(1 for lead in personalized if lead.opener_source == "fallback")
    log.info(
        "run_pipeline.summary",
        scraped=len(scraped),
        verified=len(verified),
        personalized=len(personalized),
        openers_llm=len(personalized) - fallbacks,
        openers_fallback=fallbacks,
        next_step="review this batch: run `python dashboard.py` and open http://127.0.0.1:8000",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, mock=args.mock))
