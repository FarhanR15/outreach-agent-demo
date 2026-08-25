"""Local review dashboard — the control surface for the outreach pipeline.

Run it with:

    cd tools && python dashboard.py

then open http://127.0.0.1:8000 in your browser.

What it's for: nothing reaches Instantly without passing through this page. You run the
scrape/verify/personalize stages from here, tune the targeting, the three agent roles,
the research settings and the sequence copy, read every lead and its personalized opener,
edit or reject the ones that miss, and only then push the approved set into a DRAFT
Instantly campaign.

It is deliberately local-only (binds to 127.0.0.1). It holds lead data, can trigger
metered API calls, and has no authentication of any kind — it is not something to expose
on a network.

Everything editable is stored in settings.json via config.py. Secrets are NOT: API keys
stay in .env and are never read, written or returned by any endpoint here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import config
import personalize_copy
import push_to_instantly
import scrape_apollo
import verify_apify
from _common import (
    RUN_FILE,
    Lead,
    get_logger,
    load_ledger,
    load_tmp,
    save_tmp,
    tmp_dir,
    _write_atomic,
)

log = get_logger(__name__)

app = FastAPI(title="Outreach Agent — Bombay Media")

UI_PATH = Path(__file__).parent / "dashboard_ui.html"

RUNS_FILE = "runs.json"
STATS_FILE = "agent_stats.json"
MAX_RUNS_KEPT = 50

# In-process state for the currently-running pipeline job. Single-user local tool, so a
# module-level dict is sufficient — there is never more than one operator.
job_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "message": "",
    "error": "",
    "stages_done": [],
}


# ── Request models ─────────────────────────────────────────────────────────────────────


class LeadPatch(BaseModel):
    custom_opener: str | None = None
    approved: bool | None = None


class RunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=500)
    mock: bool = True
    # Unticking research also skips personalization: with nothing researched there is
    # nothing for the qualifier to judge or the writer to reference, so the models would
    # only burn money inventing things. Leads come back with the fallback opener.
    research: bool = True
    personalize: bool = True


class SectionPayload(BaseModel):
    """A settings section. Shape is validated by the tool that consumes it, not here —
    the sections differ too much for one model, and config.save_section merges rather
    than replaces, so a partial save is safe."""

    values: dict[str, Any]


# ── Small JSON helpers for run history and stats ───────────────────────────────────────


def _read_json(filename: str, mock: bool, fallback: Any) -> Any:
    path = tmp_dir(mock) / filename
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("dashboard.unreadable_json", path=str(path))
        return fallback


def _write_json(filename: str, mock: bool, payload: Any) -> None:
    _write_atomic(tmp_dir(mock) / filename, json.dumps(payload, indent=2) + "\n")


def _record_run(mock: bool, entry: dict[str, Any]) -> None:
    """Append one run to the history, newest first, capped."""
    history = _read_json(RUNS_FILE, mock, [])
    if not isinstance(history, list):
        history = []
    history.insert(0, entry)
    _write_json(RUNS_FILE, mock, history[:MAX_RUNS_KEPT])


# ── UI ─────────────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return UI_PATH.read_text()


# ── Leads ──────────────────────────────────────────────────────────────────────────────


@app.get("/api/leads")
async def get_leads(mock: bool = True) -> dict[str, Any]:
    leads = load_tmp(RUN_FILE, mock=mock)
    scored = [lead.icp_score for lead in leads if lead.icp_score]
    return {
        "leads": [
            {**lead.model_dump(), "key": lead.dedupe_key(), "name": lead.display_name()}
            for lead in leads
        ],
        "counts": {
            "total": len(leads),
            "approved": sum(1 for lead in leads if lead.approved and not lead.pushed_to_campaign),
            "pushed": sum(1 for lead in leads if lead.pushed_to_campaign),
            "avg_score": round(sum(scored) / len(scored)) if scored else 0,
        },
    }


@app.patch("/api/leads/{key}")
async def patch_lead(key: str, patch: LeadPatch, mock: bool = True) -> dict[str, Any]:
    leads = load_tmp(RUN_FILE, mock=mock)
    for lead in leads:
        if lead.dedupe_key() != key:
            continue
        if lead.pushed_to_campaign:
            raise HTTPException(
                status_code=409,
                detail=f"{lead.display_name()} is already in campaign "
                f"'{lead.pushed_to_campaign}' and can no longer be edited.",
            )
        if patch.custom_opener is not None and patch.custom_opener != lead.custom_opener:
            lead.custom_opener = patch.custom_opener
            lead.opener_source = "edited"
        if patch.approved is not None:
            lead.approved = patch.approved
        lead.reviewed = True
        save_tmp(RUN_FILE, leads, mock=mock)
        return {"ok": True, "lead": lead.model_dump()}
    raise HTTPException(status_code=404, detail=f"No lead with key {key}")


# ── Running the pipeline ───────────────────────────────────────────────────────────────


async def _run_pipeline_job(req: RunRequest) -> None:
    started = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started": started.isoformat(),
        "mode": "test" if req.mock else "live",
        "limit": req.limit,
        "found": 0,
        "kept": 0,
        "spend": 0.0,
        "outcome": "",
    }
    try:
        job_state.update(
            running=True, stage="scrape", message="Searching Apollo…", error="", stages_done=[]
        )
        scraped = await scrape_apollo.main(limit=req.limit, mock=req.mock)
        summary["found"] = len(scraped)
        if not scraped:
            summary["outcome"] = "Apollo returned no new leads for these filters."
            job_state.update(stage="done", message=summary["outcome"])
            return
        job_state["stages_done"].append("scrape")

        if not req.research:
            # Honest shortcut: nothing was researched, so nothing is verified and no model
            # is asked to reference research that does not exist. Leads land in the review
            # queue with the fallback opener for you to write over.
            fallback = str(config.section("agents")["writer"]["fallback_opener"])
            for lead in scraped:
                lead.verified = True
                lead.verification_notes = "research skipped — this lead was not verified"
                lead.custom_opener = fallback.format(company=lead.company_name or "your business")
                lead.opener_source = "fallback"
                lead.critic_verdict = "no research, so no opener was written"
            save_tmp(RUN_FILE, scraped, mock=req.mock)
            summary["kept"] = len(scraped)
            summary["outcome"] = f"{len(scraped)} leads found. Research and writing were skipped."
            job_state.update(stage="done", message=summary["outcome"], stages_done=["scrape"])
            return

        job_state.update(
            stage="verify", message=f"Researching {len(scraped)} leads — 20–40s each…"
        )
        verified = await verify_apify.main(mock=req.mock, limit=req.limit)
        if not verified:
            summary["outcome"] = "No leads survived research."
            job_state.update(
                stage="done",
                message="No leads survived research — see the per-lead reasons in the "
                "terminal, and consider loosening the filters on the Targeting screen.",
            )
            return
        job_state["stages_done"].append("verify")

        if not req.personalize:
            summary["kept"] = len(verified)
            summary["outcome"] = f"{len(verified)} leads researched. Writing was skipped."
            job_state.update(stage="done", message=summary["outcome"])
            return

        job_state.update(
            stage="personalize",
            message=f"Judging, writing and checking openers for {len(verified)} leads…",
        )
        personalized = await personalize_copy.main(mock=req.mock)
        job_state["stages_done"].extend(["qualify", "write", "critique"])

        # Persist the per-role stats so the Agents screen still has numbers after a
        # restart. In-process only, they would vanish the moment the server stopped.
        if personalize_copy.LAST_RUN_STATS:
            _write_json(STATS_FILE, req.mock, personalize_copy.LAST_RUN_STATS)
            summary["spend"] = personalize_copy.LAST_RUN_STATS.get("total_spend", 0.0)

        fallbacks = sum(1 for lead in personalized if lead.opener_source == "fallback")
        summary["kept"] = len(personalized)
        summary["outcome"] = (
            f"Ready for review: {len(personalized)} leads"
            + (f", {fallbacks} on the fallback line" if fallbacks else "")
            + ". Nothing has been sent."
        )
        job_state.update(stage="done", message=summary["outcome"])

    except Exception as exc:  # surface the failure in the UI rather than only in logs
        log.exception("dashboard.run_failed")
        summary["outcome"] = f"{type(exc).__name__}: {exc}"
        job_state.update(stage="error", error=summary["outcome"])
    finally:
        job_state["running"] = False
        _record_run(req.mock, summary)


@app.post("/api/run")
async def run_pipeline(req: RunRequest) -> dict[str, Any]:
    if job_state["running"]:
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    asyncio.create_task(_run_pipeline_job(req))
    return {"started": True}


@app.get("/api/run-status")
async def run_status() -> dict[str, Any]:
    return job_state


@app.get("/api/runs")
async def run_history(mock: bool = True) -> dict[str, Any]:
    return {"runs": _read_json(RUNS_FILE, mock, [])}


# ── Pushing to Instantly ───────────────────────────────────────────────────────────────


@app.post("/api/push")
async def push_approved(mock: bool = True) -> dict[str, Any]:
    """Push ONLY approved, not-yet-pushed leads into a new DRAFT Instantly campaign."""
    # A pipeline run rewrites the whole run file. If one is in flight, this handler's
    # load → await → save would write a stale list back over the new batch.
    if job_state["running"]:
        raise HTTPException(
            status_code=409,
            detail="A run is in progress. Wait for it to finish before pushing.",
        )

    leads = load_tmp(RUN_FILE, mock=mock)
    to_push = [lead for lead in leads if lead.approved and not lead.pushed_to_campaign]

    if not to_push:
        raise HTTPException(
            status_code=400,
            detail="No approved leads waiting to be pushed. Approve some leads first.",
        )

    try:
        result = await push_to_instantly.push(to_push, mock=mock)
    except RuntimeError as exc:
        # Missing key, no healthy mailbox, or the draft-state safety check refusing to
        # continue. All are things the operator can act on, so say so plainly.
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Re-read and merge rather than writing our pre-await snapshot back wholesale: the
    # operator may have edited or approved other leads while Instantly was responding.
    pushed_by_key = {lead.dedupe_key(): lead.pushed_to_campaign for lead in to_push}
    current = load_tmp(RUN_FILE, mock=mock)
    for lead in current:
        stamp = pushed_by_key.get(lead.dedupe_key())
        if stamp:
            lead.pushed_to_campaign = stamp
    save_tmp(RUN_FILE, current, mock=mock)

    return {
        "pushed": result.get("lead_count", 0),
        "campaign": result.get("name", ""),
        "status": result.get("status", "draft"),
        "mock": mock,
        "note": "Campaign is a DRAFT. Open Instantly and click Launch when you're ready to send.",
    }


# ── Settings ───────────────────────────────────────────────────────────────────────────


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    """Every settings section, defaults merged with whatever has been saved."""
    return config.all_settings()


@app.put("/api/config/{name}")
async def put_config(name: str, payload: SectionPayload) -> dict[str, Any]:
    if job_state["running"]:
        raise HTTPException(
            status_code=409,
            detail="A run is in progress. Changing settings mid-run would apply them to "
            "half the batch — wait for it to finish.",
        )
    try:
        return {"ok": True, "section": name, "values": config.save_section(name, payload.values)}
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.post("/api/config/{name}/reset")
async def reset_config(name: str) -> dict[str, Any]:
    try:
        return {"ok": True, "section": name, "values": config.reset_section(name)}
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ── Read-only panels ───────────────────────────────────────────────────────────────────


@app.get("/api/ledger")
async def get_ledger(mock: bool = True, q: str = "") -> dict[str, Any]:
    """Everyone this project has ever handled.

    The ledger is keyed both by email/LinkedIn and by `apollo:<id>`; the id entries are
    lookup aliases for the same person, so they are filtered out here rather than shown
    as duplicate rows.
    """
    raw = load_ledger(mock=mock)
    rows = [
        {"key": key, **entry}
        for key, entry in raw.items()
        if not key.startswith("apollo:")
    ]
    if q:
        needle = q.lower()
        rows = [
            row for row in rows
            if needle in f"{row.get('name', '')} {row.get('company', '')} {row['key']}".lower()
        ]
    rows.sort(key=lambda row: str(row.get("at", "")), reverse=True)

    counts: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome", "unknown"))
        counts[outcome] = counts.get(outcome, 0) + 1

    return {"entries": rows, "total": len(rows), "by_outcome": counts}


@app.get("/api/stats")
async def get_stats(mock: bool = True) -> dict[str, Any]:
    """Per-role numbers from the most recent run that used the models."""
    live = personalize_copy.LAST_RUN_STATS
    return live if live else _read_json(STATS_FILE, mock, {})


@app.post("/api/estimate-pool")
async def estimate_pool() -> dict[str, Any]:
    """How many people match the current targeting. Apollo's search endpoint is free."""
    try:
        return await scrape_apollo.estimate_pool()
    except RuntimeError as exc:  # missing APOLLO_API_KEY
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Apollo did not answer: {exc}") from None


@app.get("/api/mailboxes")
async def get_mailboxes() -> dict[str, Any]:
    """How many Instantly mailboxes are healthy enough to send from. Read-only."""
    return await push_to_instantly.mailbox_health()


if __name__ == "__main__":
    import uvicorn

    print("\n  Outreach Agent — Bombay Media")
    print("  http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
