"""Shared config, paths, models, and helpers for the Outreach Agent Demo pipeline.

Two storage concepts, kept deliberately separate (this split is what prevents the same
person being contacted twice):

- **The run file** (`leads_run.json`) holds ONLY the batch currently being worked on.
  Every stage after scrape reads and writes this. It is replaced on each new run.
- **The ledger** (`ledger.json`) is a permanent, append-only record of every lead this
  project has ever seen, keyed by email/LinkedIn URL, storing what happened to them
  (pushed to a campaign, rejected in review, failed verification). Scrape and push both
  consult it so nobody is ever researched twice or emailed twice.

Mock runs use a completely separate `.tmp/mock/` directory so fixture data can never
leak into a real campaign.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator, TypeVar

import httpx
import structlog
from dotenv import load_dotenv
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

REPO_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = REPO_ROOT / ".tmp"
FIXTURES_DIR = TMP_DIR / "fixtures"
MOCK_DIR = TMP_DIR / "mock"

RUN_FILE = "leads_run.json"
LEDGER_FILE = "ledger.json"

load_dotenv(REPO_ROOT / ".env")

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)

T = TypeVar("T")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


log = get_logger(__name__)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Apollo returns this literal string rather than null for locked emails, so it passes
# naive truthiness checks. Guarded everywhere an email is validated.
PLACEHOLDER_EMAIL_MARKERS = ("email_not_unlocked", "notunlocked", "@domain.com")


def valid_email(email: str | None) -> bool:
    """The single definition of 'this address is safe to send to'.

    Used by scrape, verify AND push — a weaker check at any one of those points is how
    a placeholder or malformed address reaches a real mailbox.
    """
    if not email or not EMAIL_RE.match(email):
        return False
    return not any(marker in email.lower() for marker in PLACEHOLDER_EMAIL_MARKERS)


class Lead(BaseModel):
    """A single prospect as it flows through scrape -> verify -> personalize -> push."""

    apollo_id: str = ""  # lets the ledger skip known people BEFORE paying to enrich them
    email: str | None = None
    email_missing: bool = False
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    company_name: str = ""
    company_domain: str = ""
    linkedin_url: str = ""
    source: str = "apollo"

    verified: bool = False
    verification_notes: str = ""
    company_summary: str = ""
    linkedin_bio_snippet: str = ""
    recent_activity: str = ""
    research_notes: str = ""

    # Set by the qualifier agent — real judgement of ICP fit, replacing keyword matching.
    icp_score: int = 0
    icp_reason: str = ""

    custom_opener: str = ""
    opener_source: str = ""  # "llm" | "llm_retry" | "fallback" | "edited"
    critic_verdict: str = ""  # what the critic agent said about the final opener

    # Human review state, set from the dashboard. Only approved leads are ever pushed.
    approved: bool = False
    reviewed: bool = False

    # Set by push_to_instantly.py once the lead is in a campaign.
    pushed_to_campaign: str = ""

    def dedupe_key(self) -> str:
        return (self.email or self.linkedin_url or f"{self.first_name}:{self.company_name}").lower()

    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email or "(unknown)"


def tmp_dir(mock: bool = False) -> Path:
    """Mock runs get their own directory so fixtures never contaminate real data."""
    path = MOCK_DIR if mock else TMP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_atomic(path: Path, text: str) -> None:
    """Write via temp file + rename.

    A plain write_text that dies mid-flight leaves a truncated JSON file, and every
    later read then raises JSONDecodeError — which for the ledger means forgetting who
    we've already contacted.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text)
    os.replace(tmp_path, path)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock around a read-modify-write.

    The dashboard and a CLI run are separate processes; without this, both can read the
    ledger, and whoever writes last silently erases the other's entries — which means a
    lead already emailed gets contacted again.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def save_tmp(filename: str, leads: list[Lead], mock: bool = False) -> Path:
    path = tmp_dir(mock) / filename
    _write_atomic(path, json.dumps([lead.model_dump() for lead in leads], indent=2))
    return path


def load_tmp(filename: str, mock: bool = False) -> list[Lead]:
    path = tmp_dir(mock) / filename
    if not path.exists():
        return []
    raw: list[dict[str, Any]] = json.loads(path.read_text())
    return [Lead.model_validate(item) for item in raw]


def load_fixture(filename: str) -> list[Lead]:
    raw: list[dict[str, Any]] = json.loads((FIXTURES_DIR / filename).read_text())
    return [Lead.model_validate(item) for item in raw]


# --- Ledger: the permanent "have we already dealt with this person?" record ------------


def load_ledger(mock: bool = False) -> dict[str, dict[str, Any]]:
    path = tmp_dir(mock) / LEDGER_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # Never silently start from empty — that would mean re-contacting everyone.
        log.error("ledger.corrupt", path=str(path), hint="restore it before running again")
        raise


def ledger_record(leads: Iterable[Lead], outcome: str, mock: bool = False, **extra: Any) -> None:
    """Record what happened to these leads so later runs skip them.

    Locked and atomic: this is the file that stops people being emailed twice.
    """
    path = tmp_dir(mock) / LEDGER_FILE
    stamp = datetime.now(timezone.utc).isoformat()
    with _file_lock(path):
        ledger = load_ledger(mock)  # re-read inside the lock; another process may have written
        for lead in leads:
            entry = {
                "outcome": outcome,
                "at": stamp,
                "name": lead.display_name(),
                "company": lead.company_name,
                **extra,
            }
            ledger[lead.dedupe_key()] = entry
            # Index by Apollo id too, so the NEXT run can skip this person during the
            # free search instead of paying a credit to enrich them first.
            if lead.apollo_id:
                ledger[f"apollo:{lead.apollo_id}"] = entry
        _write_atomic(path, json.dumps(ledger, indent=2))


def ledger_index_apollo_ids(leads: Iterable[Lead], mock: bool = False) -> int:
    """Add `apollo:<id>` lookup keys WITHOUT touching the existing outcome entries.

    Used to backfill ids for people the ledger already knows by email, so the next run
    can skip them during Apollo's free search instead of paying to enrich them again.
    Deliberately does not call ledger_record: that would overwrite "pushed" (a fact we
    need to keep) with a bookkeeping marker.
    """
    path = tmp_dir(mock) / LEDGER_FILE
    added = 0
    with _file_lock(path):
        ledger = load_ledger(mock)
        for lead in leads:
            if not lead.apollo_id:
                continue
            key = f"apollo:{lead.apollo_id}"
            if key in ledger:
                continue
            existing = ledger.get(lead.dedupe_key(), {})
            ledger[key] = {
                "outcome": existing.get("outcome", "already_handled"),
                "at": existing.get("at", datetime.now(timezone.utc).isoformat()),
                "name": lead.display_name(),
                "company": lead.company_name,
                "note": "id backfilled so future searches skip this person",
            }
            added += 1
        if added:
            _write_atomic(path, json.dumps(ledger, indent=2))
    return added


def ledger_has(key: str, mock: bool = False) -> bool:
    return key in load_ledger(mock)


def ledger_known_apollo_ids(mock: bool = False) -> set[str]:
    """Apollo ids we've already handled — used to skip them before paying to enrich."""
    return {
        key.removeprefix("apollo:")
        for key in load_ledger(mock)
        if key.startswith("apollo:")
    }


def ledger_filter(leads: list[Lead], mock: bool = False) -> list[Lead]:
    """Drop leads we've already handled in a previous run."""
    ledger = load_ledger(mock)
    kept, skipped = [], []
    for lead in leads:
        already = lead.dedupe_key() in ledger or (
            lead.apollo_id and f"apollo:{lead.apollo_id}" in ledger
        )
        (skipped if already else kept).append(lead)
    if skipped:
        log.info(
            "ledger.skipped_already_handled",
            count=len(skipped),
            examples=[lead.dedupe_key() for lead in skipped[:3]],
        )
    return kept


# --- Environment ----------------------------------------------------------------------


def require_env(*names: str) -> dict[str, str]:
    """Fail fast with a readable message instead of a bare KeyError mid-run."""
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required values in .env: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill these in."
        )
    return {name: os.environ[name] for name in names}


# --- HTTP -----------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient failures only.

    Retrying a 4xx is pointless (a bad key stays bad) and actively harmful on the
    non-idempotent POSTs we make: re-running a metered Apify actor, or creating a
    duplicate Instantly campaign. Only network errors, 408, 429 and 5xx are retried.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 429) or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_retryable),
    reraise=True,  # surface the real httpx error, not tenacity's RetryError wrapper
)
async def request_json(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> Any:
    response = await client.request(method, url, **kwargs)
    if response.is_error:
        # httpx's exception message carries only the status code. APIs put the actual
        # reason ("timezone must be equal to one of the allowed values") in the body,
        # so log it — otherwise every 400 is an undiagnosable mystery.
        log.warning(
            "http.error_response",
            method=method,
            url=str(response.url).split("?")[0],  # strip query: tokens live there
            status=response.status_code,
            body=response.text[:400],
        )
    response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError:
        # A 200 carrying HTML (Cloudflare interstitial, a proxy error page) would
        # otherwise raise a non-httpx exception that no caller catches, killing the
        # whole batch. Re-raise as an httpx error so existing handlers see it.
        log.warning(
            "http.non_json_response",
            url=str(response.url).split("?")[0],
            body=response.text[:200],
        )
        raise httpx.HTTPStatusError(
            "Response was not JSON", request=response.request, response=response
        ) from None


async def gather_limited(
    items: list[T], worker: Callable[[T], Awaitable[Any]], concurrency: int = 5
) -> list[Any]:
    """Run `worker` over `items` with a hard concurrency cap.

    Unbounded asyncio.gather over hundreds of leads exhausts the httpx connection pool
    and triggers rate-limit storms against metered APIs. Five at a time is plenty.

    One item's unexpected failure must not discard the work already done on the others —
    especially in personalize, where losing the batch also loses the ledger write. Failed
    items are logged and dropped from the results.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _guarded(item: T) -> Any:
        async with sem:
            return await worker(item)

    results = await asyncio.gather(
        *(_guarded(item) for item in items), return_exceptions=True
    )

    out = []
    for item, result in zip(items, results):
        if isinstance(result, BaseException):
            log.error(
                "gather.item_failed",
                item=getattr(item, "dedupe_key", lambda: str(item)[:60])(),
                error=f"{type(result).__name__}: {result}",
            )
        else:
            out.append(result)
    return out


def campaign_name(prefix: str = "Demo - Apollo Coaches") -> str:
    """Timestamped so two runs on the same day never collide."""
    return f"{prefix} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
