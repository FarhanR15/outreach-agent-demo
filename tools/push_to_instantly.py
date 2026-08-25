"""Create a paused/draft Instantly campaign, upload leads, configure the sequence.

See workflows/build_instantly_campaign.md for the full spec.

Endpoints and payload shapes confirmed 2026-08-20 against
https://developer.instantly.ai/api-reference (base URL https://api.instantly.ai):

- POST /api/v2/campaigns — create. `status` is read-only here; new campaigns come back
  as 0 (Draft). Sending only ever starts via the separate activate/resume endpoint.
- POST /api/v2/leads/add — batched, max 1000 leads per call. Has a first-class
  `personalization` field (where our opener goes) and `skip_if_in_workspace`.

SAFETY — three independent layers stop a lead being emailed twice or unintentionally:
  1. This module never calls activate/launch/start. There is no code path to sending.
  2. `skip_if_in_workspace: true` — Instantly itself refuses a lead that exists anywhere
     else in the workspace, including campaigns this tool never created.
  3. The local ledger (see _common.py) records everyone already pushed, and the
     dashboard only ever offers un-pushed, human-approved leads.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx

import config
from _common import (
    RUN_FILE,
    Lead,
    campaign_name,
    get_logger,
    ledger_filter,
    ledger_record,
    load_tmp,
    require_env,
    request_json,
    save_tmp,
    valid_email,
)

log = get_logger(__name__)

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
STATUS_DRAFT = 0
STATUS_PAUSED = 2
NOT_SENDING_STATUSES = (STATUS_DRAFT, STATUS_PAUSED)
LEADS_PER_CALL = 1000  # documented hard cap

# Account status enums, from Instantly's OpenAPI spec.
ACCOUNT_ACTIVE = 1
WARMUP_BANNED = -1
WARMUP_PERMANENT_SUSPENSION = -3

def campaign_settings() -> dict[str, Any]:
    """Sequence copy, cap, timezone and toggles, from the dashboard's Campaign screen.

    The timezone is worth knowing about: Instantly accepts only a fixed enum and rejects
    the campaign with a 400 otherwise. "America/New_York" is NOT in it — America/Detroit
    is the Eastern-time entry that is. Verified against the live API 2026-08-20.
    """
    settings = config.section("campaign")
    settings["timezone"] = config.with_env("campaign", "timezone", "INSTANTLY_TIMEZONE")
    return settings


def _is_sendable_account(account: dict) -> bool:
    """Is this mailbox safe to send from right now?

    Instantly doesn't publish a canonical "safe to send" rule, so this is assembled from
    the documented status enums, erring conservative:
      status == 1                  Active (not paused, not in maintenance, not errored)
      warmup_status not in (-1,-3) not Banned, not Permanently Suspended
      setup_pending == False       finished connecting
      autofix_failed != True       reconnection hasn't permanently failed
    """
    return (
        account.get("status") == ACCOUNT_ACTIVE
        and account.get("warmup_status") not in (WARMUP_BANNED, WARMUP_PERMANENT_SUSPENSION)
        and not account.get("setup_pending")
        and account.get("autofix_failed") is not True
        and bool(account.get("email"))
    )


async def discover_sending_mailboxes(client: httpx.AsyncClient, api_key: str) -> list[str]:
    """Ask Instantly which mailboxes exist and are healthy, instead of hardcoding them.

    Set INSTANTLY_SENDING_EMAILS in .env to override with a specific list.
    """
    override = os.environ.get("INSTANTLY_SENDING_EMAILS", "").strip()
    if override:
        emails = [e.strip() for e in override.split(",") if e.strip()]
        log.info("push.mailboxes_from_env", count=len(emails))
        return emails

    accounts: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100, "status": ACCOUNT_ACTIVE}
        if cursor:
            params["starting_after"] = cursor
        payload = await request_json(
            client, "GET", f"{INSTANTLY_BASE}/accounts",
            headers={"Authorization": f"Bearer {api_key}"}, params=params,
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        accounts.extend(items)
        cursor = payload.get("next_starting_after") if isinstance(payload, dict) else None
        if not cursor or not items:
            break

    sendable = [a["email"] for a in accounts if _is_sendable_account(a)]
    skipped = len(accounts) - len(sendable)
    log.info("push.mailboxes_discovered", usable=len(sendable), skipped_unhealthy=skipped)

    if not sendable:
        raise RuntimeError(
            "No healthy sending mailboxes found in your Instantly workspace. Check the "
            "Accounts page — they may be paused, still connecting, or in an error state. "
            "You can also set INSTANTLY_SENDING_EMAILS in .env to choose explicitly."
        )
    return sendable


async def mailbox_health() -> dict[str, Any]:
    """How many mailboxes are healthy enough to send from. Read-only.

    Shown in the dashboard's sidebar so an empty or degraded workspace is visible before
    a push fails rather than after. Never raises: a dashboard panel is not worth a 500,
    so a missing key or an unreachable API reports itself as an error string instead.
    """
    try:
        cfg = require_env("INSTANTLY_API_KEY")
    except RuntimeError as exc:
        return {"usable": 0, "error": str(exc)}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            emails = await discover_sending_mailboxes(client, cfg["INSTANTLY_API_KEY"])
        return {"usable": len(emails)}
    except (httpx.HTTPError, RuntimeError) as exc:
        return {"usable": 0, "error": str(exc)}


async def assert_not_sending(client: httpx.AsyncClient, api_key: str, campaign_id: str) -> str:
    """Read the campaign back after creation and confirm it is not sending.

    Belt-and-braces: we already never call activate, but this proves the end state
    rather than assuming it.
    """
    payload = await request_json(
        client, "GET", f"{INSTANTLY_BASE}/campaigns/{campaign_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    campaign = _extract_campaign(payload) if isinstance(payload, dict) else {}
    status = campaign.get("status")
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        status_int = None

    if status_int not in NOT_SENDING_STATUSES:
        raise RuntimeError(
            f"SAFETY CHECK FAILED: campaign {campaign_id} reads back with status="
            f"{status!r}, which is not Draft(0) or Paused(2). Go pause or delete it in "
            "Instantly immediately."
        )
    return "Draft" if status_int == STATUS_DRAFT else "Paused"


def _extract_campaign(payload: dict) -> dict:
    """Tolerate either a bare campaign object or one wrapped in a key."""
    if "id" in payload:
        return payload
    for key in ("campaign", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict) and "id" in nested:
            return nested
    raise RuntimeError(f"Could not find a campaign id in Instantly's response: {payload!r}")


def _assert_draft(campaign: dict) -> None:
    """Fail closed: refuse to continue unless Instantly confirms the campaign is a draft."""
    raw_status = campaign.get("status")
    try:
        status = int(raw_status)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Instantly returned a non-numeric campaign status {raw_status!r}; refusing "
            "to proceed because this pipeline must never touch a sending campaign."
        )
    if status != STATUS_DRAFT:
        raise RuntimeError(
            f"Instantly campaign came back with status={status} (expected "
            f"{STATUS_DRAFT}=Draft). Refusing to proceed — this pipeline must never "
            "create or feed an actively-sending campaign."
        )


async def _create_campaign(
    client: httpx.AsyncClient, api_key: str, name: str, sending_emails: list[str]
) -> dict:
    settings = campaign_settings()
    body = {
        "name": name,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "Business hours",
                    "timing": {"from": settings["send_from"], "to": settings["send_to"]},
                    "days": {str(d): d < 5 for d in range(7)},
                    "timezone": settings["timezone"],
                }
            ]
        },
        "sequences": [
            {
                "steps": [
                    {
                        "type": "email",
                        "delay": step["delay"],
                        "delay_unit": "days",
                        "variants": [{"subject": step["subject"], "body": step["body"]}],
                    }
                    for step in settings["steps"]
                ]
            }
        ],
        "email_list": sending_emails,
        "daily_limit": int(settings["daily_limit"]),
        "stop_on_reply": bool(settings["stop_on_reply"]),
        "stop_on_auto_reply": bool(settings["stop_on_reply"]),
        "insert_unsubscribe_header": bool(settings["insert_unsubscribe_header"]),
    }
    payload = await request_json(
        client,
        "POST",
        f"{INSTANTLY_BASE}/campaigns",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
    )
    campaign = _extract_campaign(payload)
    _assert_draft(campaign)
    return campaign


async def _add_leads_chunk(
    client: httpx.AsyncClient, api_key: str, campaign_id: str, leads: list[Lead]
) -> int:
    body = {
        "campaign_id": campaign_id,
        "skip_if_in_workspace": True,
        "leads": [
            {
                "email": lead.email,
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "company_name": lead.company_name,
                "personalization": lead.custom_opener,
            }
            for lead in leads
        ],
    }
    result = await request_json(
        client,
        "POST",
        f"{INSTANTLY_BASE}/leads/add",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
    )
    if isinstance(result, dict):
        for key in ("leads_uploaded", "total_sent", "created"):
            if isinstance(result.get(key), int):
                return result[key]
        log.warning("push.upload_count_unknown", response_keys=list(result.keys()))
    return 0


async def push(leads: list[Lead], mock: bool) -> dict:
    name = campaign_name(str(campaign_settings()["name_prefix"]))

    # Never upload a lead without a usable email, whatever earlier stages believed. Uses
    # the same shared validator as scrape and verify — a weaker check here is how a
    # placeholder like email_not_unlocked@domain.com reaches a real mailbox.
    sendable = [lead for lead in leads if valid_email(lead.email)]
    if len(sendable) < len(leads):
        log.warning("push.skipped_leads_without_email", count=len(leads) - len(sendable))
    if not sendable:
        return {"name": name, "status": "draft", "lead_count": 0, "note": "no sendable leads"}

    if mock:
        log.info("push.mock", campaign_name=name, lead_count=len(sendable))
        for lead in sendable:
            log.info("push.mock_lead", lead=lead.dedupe_key(), opener=lead.custom_opener)
        # Record in the mock ledger too, so test runs exercise the same
        # never-contact-twice path the real runs rely on. The mock ledger lives in
        # .tmp/mock/ and can never affect a real campaign.
        for lead in sendable:
            lead.pushed_to_campaign = name
        ledger_record(sendable, outcome="pushed", mock=True, campaign=name)
        return {"name": name, "status": "draft", "lead_count": len(sendable), "mock": True}

    cfg = require_env("INSTANTLY_API_KEY")
    api_key = cfg["INSTANTLY_API_KEY"]

    async with httpx.AsyncClient(timeout=60) as client:
        sending_emails = await discover_sending_mailboxes(client, api_key)
        campaign = await _create_campaign(client, api_key, name, sending_emails)
        campaign_id = campaign["id"]

        uploaded = 0
        pushed_leads: list[Lead] = []
        for start in range(0, len(sendable), LEADS_PER_CALL):
            chunk = sendable[start : start + LEADS_PER_CALL]
            try:
                uploaded += await _add_leads_chunk(client, api_key, campaign_id, chunk)
                pushed_leads.extend(chunk)
            except httpx.HTTPError as exc:
                log.error("push.chunk_failed", start=start, error=str(exc))

    # Record the push BEFORE the safety read-back. These leads are already in Instantly;
    # if the read-back then fails (timeout, 500), an exception must not prevent us
    # remembering that they were contacted — that's exactly how someone gets emailed
    # twice.
    for lead in pushed_leads:
        lead.pushed_to_campaign = name
    ledger_record(pushed_leads, outcome="pushed", mock=mock, campaign=name)

    async with httpx.AsyncClient(timeout=60) as client:
        # Prove the campaign really isn't sending, rather than assuming it.
        final_state = await assert_not_sending(client, api_key, campaign_id)

    log.info(
        "push.done",
        campaign_name=name,
        campaign_id=campaign_id,
        uploaded=uploaded,
        submitted=len(sendable),
        mailboxes=len(sending_emails),
        verified_state=final_state,
        next_step="open Instantly and click Launch when you're ready to send",
    )
    return {
        "name": name,
        "id": campaign_id,
        "status": final_state.lower(),
        "lead_count": uploaded,
        "submitted": len(sendable),
        "mailboxes": len(sending_emails),
    }


async def main(mock: bool = False) -> dict:
    """CLI entry point. Pushes ONLY leads a human approved in the dashboard.

    Running this twice must be safe, so the results are written back to the run file —
    without that, `pushed_to_campaign` stays empty in the file and a second run would
    create a duplicate campaign with the same people.
    """
    leads = load_tmp(RUN_FILE, mock=mock)
    to_push = [
        lead for lead in leads
        if lead.approved and not lead.pushed_to_campaign
    ]
    to_push = ledger_filter(to_push, mock=mock)

    if not to_push:
        log.warning(
            "push.nothing_to_do",
            total_in_batch=len(leads),
            hint="approve leads in the dashboard first (python dashboard.py)",
        )
        return {"name": "", "status": "draft", "lead_count": 0}

    result = await push(to_push, mock=mock)
    save_tmp(RUN_FILE, leads, mock=mock)  # persist pushed_to_campaign stamps
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(mock=args.mock))
