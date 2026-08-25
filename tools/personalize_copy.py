"""Qualify, write, and critique — three models, one opener per lead.

See workflows/personalize_copy.md for the full spec and agents.py for the model roles.

Per lead:
  1. QUALIFIER decides whether this person is genuinely an online coach/course creator
     worth contacting. Leads it rejects are dropped here, before we spend a writer call
     on them — this is both cheaper and a far better ICP filter than keyword matching.
  2. WRITER drafts one opening sentence from the research.
  3. CRITIC (a different model family) checks that sentence against the research for
     fabrication and generic AI filler. On a fail, the writer gets ONE retry with the
     critic's reason as feedback. On a second fail, we use a bland-but-safe fallback.

The opener is deliberately not a full email rewrite: the sequence copy in
context/messaging.md stays fixed so the voice stays consistent, and only the hook is
per-person.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

import httpx

from _common import (
    RUN_FILE,
    Lead,
    gather_limited,
    get_logger,
    ledger_record,
    load_tmp,
    require_env,
    save_tmp,
)
import config
from agents import (
    CRITIC,
    QUALIFIER,
    WRITER,
    CostTracker,
    call_role,
    parse_json_reply,
)

log = get_logger(__name__)

# What the last run cost and how each role performed. The dashboard reads this to fill
# the per-role stats on its Agents screen; nothing in the pipeline depends on it.
LAST_RUN_STATS: dict[str, Any] = {}

# Local guards that run before the critic — cheap, deterministic, catch the obvious.
FABRICATION_PATTERNS = (
    re.compile(r"\$\s?\d"),
    re.compile(r"\b\d+\s?(k|m)\b", re.I),
    re.compile(r"\d+\s?(followers|subscribers|students|clients|members)", re.I),
    re.compile(r"\b\d+\s?%"),
)


def _research_blob(lead: Lead) -> str:
    parts = []
    if lead.recent_activity:
        parts.append(f"RECENT POSTS:\n{lead.recent_activity}")
    if lead.linkedin_bio_snippet:
        parts.append(f"LINKEDIN BIO:\n{lead.linkedin_bio_snippet}")
    if lead.company_summary:
        parts.append(f"THEIR WEBSITE:\n{lead.company_summary[:1200]}")
    parts.append(
        f"NAME: {lead.display_name()}\nTITLE: {lead.title}\nCOMPANY: {lead.company_name}"
    )
    return "\n\n".join(parts)


def _local_opener_problems(text: str, max_words: int) -> str:
    """Deterministic checks. Returns a reason string, or "" if the line looks fine."""
    if not text:
        return "empty"
    if text.strip().upper().startswith("INSUFFICIENT"):
        return "model_said_research_too_thin"
    if len(text.split()) > max_words:
        return "too_long"
    if "\n" in text.strip():
        return "multi_line"
    if any(pattern.search(text) for pattern in FABRICATION_PATTERNS):
        return "contains_unverifiable_number"
    return ""


async def _qualify(client, api_key, lead: Lead, research: str, costs: CostTracker) -> bool:
    """True if this lead should proceed. Infrastructure failures fail OPEN (keep the
    lead, flag it) — a flaky API call shouldn't silently shrink the batch."""
    settings = QUALIFIER.settings()
    reply = await call_role(client, api_key, QUALIFIER, QUALIFIER.system, research, costs)
    verdict = parse_json_reply(reply)

    if verdict is None:
        lead.icp_score, lead.icp_reason = 0, "qualifier unavailable — needs your eye"
        return True

    lead.icp_score = int(verdict.get("score") or 0)
    lead.icp_reason = str(verdict.get("reason") or "")[:200]
    return bool(verdict.get("fit")) and lead.icp_score >= int(settings.get("min_score", 50))


async def _write_and_check(
    client, api_key, lead: Lead, research: str, costs: CostTracker
) -> None:
    """Writer → critic → (one retry) → fallback. Sets custom_opener and opener_source."""
    settings = WRITER.settings()
    fallback = str(settings["fallback_opener"]).format(
        company=lead.company_name or "your business"
    )
    max_words = int(settings.get("max_opener_words", 28))
    attempts = 1 + max(0, int(settings.get("retries", 1)))
    feedback = ""

    for attempt in range(1, attempts + 1):
        prompt = research if not feedback else (
            f"{research}\n\nYour previous attempt was rejected because: {feedback}\n"
            "Write a better one that fixes that specific problem."
        )
        draft = await call_role(client, api_key, WRITER, WRITER.system, prompt, costs)
        draft = (draft or "").strip().strip('"')

        problem = _local_opener_problems(draft, max_words)
        if problem:
            feedback = problem
            log.info("personalize.local_reject", lead=lead.display_name(), reason=problem, attempt=attempt)
            continue

        critique = parse_json_reply(
            await call_role(
                client, api_key, CRITIC, CRITIC.system,
                f"RESEARCH:\n{research}\n\nSENTENCE TO REVIEW:\n{draft}",
                costs,
            )
        )

        if critique is None:
            # Critic unavailable — the line already passed the local guards, so accept it
            # but record that it went unreviewed.
            lead.custom_opener = draft
            lead.opener_source = "llm" if attempt == 1 else "llm_retry"
            lead.critic_verdict = "critic unavailable — not independently reviewed"
            return

        if critique.get("pass"):
            lead.custom_opener = draft
            lead.opener_source = "llm" if attempt == 1 else "llm_retry"
            lead.critic_verdict = f"passed: {str(critique.get('reason') or '')[:160]}"
            return

        feedback = str(critique.get("reason") or "not specific enough")[:200]
        log.info("personalize.critic_reject", lead=lead.display_name(), reason=feedback, attempt=attempt)

    lead.custom_opener = fallback
    lead.opener_source = "fallback"
    lead.critic_verdict = f"fell back after {attempts} attempts: {feedback}"[:200]


async def _process_one(client, api_key: str, lead: Lead, costs: CostTracker) -> Lead:
    research = _research_blob(lead)

    if len(research) < int(config.section("research")["min_research_chars"]):
        lead.custom_opener = str(WRITER.settings()["fallback_opener"]).format(
            company=lead.company_name or "your business"
        )
        lead.opener_source = "fallback"
        lead.icp_reason = "not enough research to judge or personalize"
        return lead

    if not await _qualify(client, api_key, lead, research, costs):
        lead.verified = False
        lead.verification_notes = f"qualifier rejected: {lead.icp_reason}"
        return lead

    await _write_and_check(client, api_key, lead, research, costs)
    return lead


async def personalize(leads: list[Lead], mock: bool) -> list[Lead]:
    if mock:
        for lead in leads:
            lead.icp_score, lead.icp_reason = 82, "mock: sells a paid coaching program"
            lead.custom_opener = (
                f"Saw you're running a cohort over at {lead.company_name} — mock opener."
            )
            lead.opener_source = "llm"
            lead.critic_verdict = "passed: mock"
        log.info("personalize.mock", count=len(leads))
        return leads

    cfg = require_env("OPENROUTER_API_KEY")
    costs = CostTracker()

    async with httpx.AsyncClient(timeout=90) as client:
        results = await gather_limited(
            leads,
            lambda lead: _process_one(client, cfg["OPENROUTER_API_KEY"], lead, costs),
            concurrency=int(config.section("research")["concurrency"]),
        )

    kept = [lead for lead in results if lead.verified]
    rejected = [lead for lead in results if not lead.verified]
    if rejected:
        for lead in rejected:
            log.info("personalize.qualifier_dropped", lead=lead.display_name(), reason=lead.icp_reason)
        ledger_record(rejected, outcome="rejected_by_qualifier", mock=mock)

    by_source: dict[str, int] = {}
    for lead in kept:
        by_source[lead.opener_source] = by_source.get(lead.opener_source, 0) + 1

    _record_stats(results, kept, rejected, by_source, costs)

    log.info(
        "personalize.done",
        kept=len(kept),
        dropped_by_qualifier=len(rejected),
        openers=by_source,
        models={"qualifier": QUALIFIER.model, "writer": WRITER.model, "critic": CRITIC.model},
        usage=costs.summary(),
    )
    return kept


# OpenRouter prices per 1M tokens, as of 2026-08-20. Used only to show an approximate
# spend in the dashboard — the invoice is the real number, not this.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-5.6-luna": (0.20, 1.20),
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "google/gemini-3.7-flash": (0.375, 1.875),
    "nvidia/nemotron-3.5-lightning": (0.04, 0.20),
}


def _role_spend(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Approximate dollar cost for one role. Unknown models report 0 rather than guess."""
    prices = MODEL_PRICES.get(model)
    if not prices:
        return 0.0
    return (prompt_tokens * prices[0] + completion_tokens * prices[1]) / 1_000_000


def _record_stats(
    results: list[Lead],
    kept: list[Lead],
    rejected: list[Lead],
    by_source: dict[str, int],
    costs: CostTracker,
) -> None:
    """Fill LAST_RUN_STATS for the dashboard's Agents screen.

    Counted from the leads themselves rather than instrumenting each call site: the
    numbers a human wants ("how many did the critic send back?") are visible in the
    outcome, and deriving them here keeps the hot path untouched.
    """
    usage = costs.summary()
    retried = by_source.get("llm_retry", 0)
    fell_back = by_source.get("fallback", 0)
    written = len(kept)

    def role_block(role_name: str, model: str, **extra: Any) -> dict[str, Any]:
        prompt_tokens = usage["prompt_tokens"].get(role_name, 0)
        completion_tokens = usage["completion_tokens"].get(role_name, 0)
        return {
            "model": model,
            "calls": usage["calls"].get(role_name, 0),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "spend": round(_role_spend(model, prompt_tokens, completion_tokens), 4),
            **extra,
        }

    LAST_RUN_STATS.clear()
    LAST_RUN_STATS.update(
        {
            "qualifier": role_block(
                "qualifier", QUALIFIER.model,
                judged=len(results), passed=len(results) - len(rejected), rejected=len(rejected),
            ),
            "writer": role_block(
                "writer", WRITER.model,
                written=written, retried=retried, fell_back=fell_back,
            ),
            "critic": role_block(
                "critic", CRITIC.model,
                # The critic sends a line back once per retry and once per fallback; the
                # rest of its reviews passed.
                reviewed=usage["calls"].get("critic", 0),
                rejected=retried + fell_back,
            ),
            "total_spend": round(
                sum(
                    _role_spend(
                        role.model,
                        usage["prompt_tokens"].get(role.name, 0),
                        usage["completion_tokens"].get(role.name, 0),
                    )
                    for role in (QUALIFIER, WRITER, CRITIC)
                ),
                4,
            ),
        }
    )


async def main(mock: bool = False) -> list[Lead]:
    leads = load_tmp(RUN_FILE, mock=mock)
    personalized = await personalize(leads, mock=mock)
    save_tmp(RUN_FILE, personalized, mock=mock)
    return personalized


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(mock=args.mock))
