"""Multi-model agent layer — three specialised roles, each on its own model.

Why three models instead of one:

  QUALIFIER  cheap + fast   Reads the research and judges whether this person really is
                            an online coach/course creator worth contacting. Replaces the
                            old crude keyword match. Runs on every lead, so cost matters
                            more than brilliance.
  WRITER     quality-first  Writes the one opening sentence. This is the only step whose
                            output a stranger actually reads, so it gets the best model.
  CRITIC     independent    Checks the writer's sentence against the research for
                            fabrication and AI-slop. Deliberately a DIFFERENT model
                            family from the writer — a model reviewing its own output
                            shares its own blind spots.

If the critic rejects, the writer gets one retry with the critic's reason as feedback.
If it rejects again, we fall back to a bland-but-safe line rather than sending a
confident-sounding fabrication to a real person.

Every role's model, token budget, temperature and instructions live in settings.json and
are editable from the dashboard (see config.py). A role reads its settings fresh on every
call, so changing one in the dashboard takes effect on the next run without a restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

import config
from _common import get_logger, request_json

log = get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# Strict JSON schemas. OpenRouter supports two levels of structured output:
# `json_object` (valid JSON, no schema) and `json_schema` (enforced shape). We use the
# stronger one — a malformed qualifier verdict silently costs us good leads.
QUALIFIER_SCHEMA = {
    "name": "qualification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "fit": {"type": "boolean"},
            "score": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["fit", "score", "reason"],
        "additionalProperties": False,
    },
}

CRITIC_SCHEMA = {
    "name": "critique",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "pass": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["pass", "reason"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class Role:
    """One model role. Everything tunable is read from settings.json on access.

    The JSON schema stays in code rather than settings: it has to match what
    `parse_json_reply` and its callers expect, so it is not something to edit loosely.
    """

    name: str  # "qualifier" | "writer" | "critic" — the key in the agents settings
    json_schema: dict[str, Any] | None = None

    def settings(self) -> dict[str, Any]:
        return config.section("agents")[self.name]

    @property
    def model(self) -> str:
        return config.model_for(self.name)

    @property
    def max_tokens(self) -> int:
        return int(self.settings()["max_tokens"])

    @property
    def temperature(self) -> float | None:
        """None means "don't send a temperature at all". Several current frontier models
        (reasoning models, and Claude Sonnet 5 on OpenRouter) reject the parameter."""
        value = self.settings().get("temperature")
        return None if value is None else float(value)

    @property
    def system(self) -> str:
        return str(self.settings()["system"])


# Defaults for all three live in config.DEFAULTS["agents"] — slugs verified 2026-08-20 to
# exist on OpenRouter, with pricing confirmed from openrouter.ai/api/v1/models.
# Deliberately three different vendors: a critic sharing the writer's family would share
# its blind spots.
QUALIFIER = Role(name="qualifier", json_schema=QUALIFIER_SCHEMA)
WRITER = Role(name="writer")
CRITIC = Role(name="critic", json_schema=CRITIC_SCHEMA)


class CostTracker:
    """Running tally of what a pipeline run actually cost, per role.

    OpenRouter returns usage on every response; surfacing the total in the run summary
    is how we keep an eye on spend instead of discovering it on the invoice.
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.prompt_tokens: dict[str, int] = {}
        self.completion_tokens: dict[str, int] = {}

    def record(self, role: str, usage: dict[str, Any] | None) -> None:
        self.calls[role] = self.calls.get(role, 0) + 1
        if not usage:
            return
        self.prompt_tokens[role] = self.prompt_tokens.get(role, 0) + int(usage.get("prompt_tokens") or 0)
        self.completion_tokens[role] = self.completion_tokens.get(role, 0) + int(usage.get("completion_tokens") or 0)

    def summary(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "prompt_tokens": dict(self.prompt_tokens),
            "completion_tokens": dict(self.completion_tokens),
            "total_tokens": sum(self.prompt_tokens.values()) + sum(self.completion_tokens.values()),
        }


async def call_role(
    client: httpx.AsyncClient,
    api_key: str,
    role: Role,
    system: str,
    user: str,
    costs: CostTracker | None = None,
) -> str | None:
    """One completion from one role. Returns None on any failure — callers decide."""
    body: dict[str, Any] = {
        "model": role.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": role.max_tokens,
    }
    if role.temperature is not None:
        body["temperature"] = role.temperature
    if role.json_schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": role.json_schema}
        # Structured-output support is per-ENDPOINT, not per-model: the same slug served
        # by a different provider may not honour the schema. This pins routing to
        # providers that actually support what we're asking for.
        body["provider"] = {"require_parameters": True}

    try:
        data = await request_json(
            client,
            "POST",
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
    except httpx.HTTPError as exc:
        log.warning("agents.call_failed", role=role.name, model=role.model, error=str(exc))
        return None

    if costs is not None:
        costs.record(role.name, data.get("usage"))

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        log.warning("agents.malformed_response", role=role.name, model=role.model)
        return None

    # A truncated reply looks like a parse failure downstream, which reads as "the model
    # was unavailable" and silently skips the check. Name it for what it is.
    if choice.get("finish_reason") == "length":
        log.warning(
            "agents.response_truncated",
            role=role.name,
            model=role.model,
            max_tokens=role.max_tokens,
            hint="raise max_tokens for this role — reasoning tokens count toward it",
        )
    return content


def parse_json_reply(text: str | None) -> dict[str, Any] | None:
    """Parse a model's JSON reply, tolerating markdown fences and surrounding prose."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(cleaned[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
    log.warning("agents.json_parse_failed", got=(text or "")[:160])
    return None


# --- Prompts ---------------------------------------------------------------------------
#
# The three system prompts now live in settings.json so they can be edited from the
# dashboard, with the originals as defaults in config.DEFAULTS["agents"]. Reach them
# through the role: QUALIFIER.system, WRITER.system, CRITIC.system.
