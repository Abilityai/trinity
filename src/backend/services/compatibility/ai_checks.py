"""
AI-evaluated compatibility checks (#668).

The genuinely-judgment checks (does this CLAUDE.md have domain-specific
instructions? are these use_cases realistic prompts?) are evaluated by an LLM.
To keep cost and latency bounded these are **batched by category** into a few
concurrent Anthropic calls (Haiku), each forced through a tool-use schema so the
result is structured per check-id.

Robustness rules (do not relax):
  * Iterate the EXPECTED check-id set from `spec`, not what the model returned —
    a check the model omits becomes "skipped" (reason "ai_no_result"), never
    silently vanishes.
  * Per-item validation — one malformed item is dropped, the rest survive.
  * Fail-open — no API key, API error, or parse failure → that category's checks
    are "skipped" with a reason, distinct from "passed".
  * Secret safety — `.env` / generated `.mcp.json` content is never in the
    snapshot; everything that IS sent is run through a secret-redaction pass
    first, and per-file/total length caps keep the prompt bounded.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from services.settings_service import get_anthropic_api_key
from . import spec

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_PER_CALL_TIMEOUT = 30.0
_MAX_FILE_CHARS = 8000      # per file in the AI bundle
_MAX_SKILL_FILES = 12       # cap skill files included
_MAX_BUNDLE_CHARS = 48000   # hard ceiling on the whole bundle

# Files safe to send to the AI (placeholders / instructions only — never .env or
# the generated .mcp.json, which are existence-only in the snapshot anyway).
_AI_SAFE_FILES = [
    "template.yaml", "CLAUDE.md", "AGENTS.md", ".env.example",
    ".mcp.json.template", "dashboard.yaml", "README.md",
]

# Reuse the secret detector for a redaction pass before egress.
from .static_checks import _SECRET_PATTERNS, _ASSIGN_RE, _looks_placeholder  # noqa: E402


def _redact(text: str) -> str:
    """Replace anything resembling a secret with a marker before sending to AI."""
    if not text:
        return text
    redacted = text
    for rx, _label in _SECRET_PATTERNS:
        redacted = rx.sub("[REDACTED]", redacted)

    def _assign_sub(m):
        if _looks_placeholder(m.group(2)):
            return m.group(0)
        return f"{m.group(1)}=[REDACTED]"

    return _ASSIGN_RE.sub(_assign_sub, redacted)


def _build_bundle(snapshot: Dict[str, Any]) -> str:
    """A bounded, redacted text bundle of the agent's source for AI context."""
    parts: List[str] = []
    total = 0

    def add(name: str, content: Optional[str]):
        nonlocal total
        if not content or total >= _MAX_BUNDLE_CHARS:
            return
        body = _redact(content)[:_MAX_FILE_CHARS]
        block = f"\n===== {name} =====\n{body}\n"
        if total + len(block) > _MAX_BUNDLE_CHARS:
            block = block[: _MAX_BUNDLE_CHARS - total]
        parts.append(block)
        total += len(block)

    files = snapshot.get("files") or {}
    for path in _AI_SAFE_FILES:
        info = files.get(path) or {}
        if info.get("content"):
            add(path, info["content"])

    skills = snapshot.get("skills") or {}
    for i, (rel, info) in enumerate(sorted(skills.items())):
        if i >= _MAX_SKILL_FILES:
            break
        if info.get("content"):
            add(rel, info["content"])

    return "".join(parts) if parts else "(no readable source files)"


_TOOL = {
    "name": "report_compatibility",
    "description": "Report PASS/FAIL verdicts for the listed compatibility checks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "check_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["pass", "fail"]},
                        "explanation": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["check_id", "status"],
                },
            }
        },
        "required": ["results"],
    },
}


def _category_prompt(checks: List[spec.CheckDef], bundle: str) -> str:
    lines = [
        "You are validating a Trinity agent's source files against best-practice checks.",
        "For EACH check id below, decide PASS or FAIL based ONLY on the provided files.",
        "Be conservative: if a file needed for a check is absent, PASS it (another check covers presence).",
        "Return your verdicts via the report_compatibility tool — one entry per check id.",
        "",
        "CHECKS:",
    ]
    for c in checks:
        lines.append(f"- {c.id}: {c.prompt}")
    lines.append("")
    lines.append("AGENT SOURCE FILES:")
    lines.append(bundle)
    return "\n".join(lines)


async def _call_category(
    client: httpx.AsyncClient, api_key: str, checks: List[spec.CheckDef], bundle: str
) -> Dict[str, Dict[str, Any]]:
    """One Anthropic call for one category; returns {check_id: {status, explanation, confidence}}.

    Returns {} on any failure (caller turns missing ids into 'skipped').
    """
    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 1500,
                "tools": [_TOOL],
                "tool_choice": {"type": "tool", "name": "report_compatibility"},
                "messages": [{"role": "user", "content": _category_prompt(checks, bundle)}],
            },
            timeout=_PER_CALL_TIMEOUT,
        )
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.warning("[compatibility] AI category call failed: %s", e)
        return {}

    if resp.status_code != 200:
        logger.warning("[compatibility] AI category HTTP %s: %s", resp.status_code, resp.text[:200])
        return {}

    try:
        data = resp.json()
        results: Dict[str, Dict[str, Any]] = {}
        for block in data.get("content", []):
            if block.get("type") != "tool_use":
                continue
            for item in (block.get("input") or {}).get("results", []):
                if not isinstance(item, dict):
                    continue
                cid = item.get("check_id")
                status = item.get("status")
                if cid not in spec.BY_ID or status not in ("pass", "fail"):
                    continue  # drop malformed items individually
                conf = item.get("confidence")
                results[cid] = {
                    "status": status,
                    "explanation": (item.get("explanation") or "")[:600] or None,
                    "confidence": float(conf) if isinstance(conf, (int, float)) else None,
                }
        return results
    except (ValueError, TypeError, KeyError) as e:
        logger.warning("[compatibility] AI response parse failed: %s", e)
        return {}


async def run_ai(snapshot: Dict[str, Any], ai_check_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Evaluate the requested AI checks; returns {check_id: verdict}.

    A verdict is {status: pass|fail|skipped, explanation, confidence, skip_reason?}.
    Every requested id is present in the result (iterate-expected guarantee).
    """
    requested = [cid for cid in ai_check_ids if cid in spec.BY_ID]
    out: Dict[str, Dict[str, Any]] = {}

    api_key = get_anthropic_api_key()
    if not api_key:
        for cid in requested:
            out[cid] = {"status": "skipped", "skip_reason": "no_api_key",
                        "explanation": None, "confidence": None}
        return out

    # Group requested checks by category.
    by_cat: Dict[str, List[spec.CheckDef]] = {}
    for cid in requested:
        c = spec.BY_ID[cid]
        by_cat.setdefault(c.category, []).append(c)

    bundle = _build_bundle(snapshot)

    async with httpx.AsyncClient() as client:
        tasks = [_call_category(client, api_key, checks, bundle) for checks in by_cat.values()]
        category_results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: Dict[str, Dict[str, Any]] = {}
    for res in category_results:
        if isinstance(res, dict):
            merged.update(res)

    # Iterate the EXPECTED set — anything the model omitted is 'skipped'.
    for cid in requested:
        if cid in merged:
            out[cid] = merged[cid]
        else:
            out[cid] = {"status": "skipped", "skip_reason": "ai_no_result",
                        "explanation": None, "confidence": None}
    return out
