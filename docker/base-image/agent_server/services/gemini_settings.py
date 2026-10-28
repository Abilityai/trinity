"""Platform-owned keys in the Gemini CLI's user settings (trinity-enterprise#986).

Gemini CLI defaults ``privacy.usageStatisticsEnabled`` to ``true``. With that
default a model turn creates ``~/.gemini/installation_id`` and sends usage
events carrying it (plus CPU model, core count, memory, OS and kernel) to
Google, and adds the same id as ``x-gemini-api-privileged-user-id`` on every
model request. Trinity's rule is that nothing leaves the server without an
admin-level opt-in, so the platform writes the opt-out and an agent cannot turn
it back on.

Why the USER file: in the pinned CLI (0.62.0) the same key in the system file
(``/etc/gemini-cli/settings.json`` / ``GEMINI_CLI_SYSTEM_SETTINGS_PATH``) does
not stop the id from being created; ``~/.gemini/settings.json`` does. Only a
model turn creates it, so :func:`ensure_usage_statistics_disabled` runs at
startup and immediately before every Gemini turn spawn, because the agent can
rewrite its own settings between turns.

Leaf module: stdlib only, never raises.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolved at call time (never bound as a default argument) so tests can
# redirect it.
GEMINI_SETTINGS_PATH = Path("/home/developer/.gemini/settings.json")


def apply_privacy_defaults(settings: dict) -> bool:
    """Set ``privacy.usageStatisticsEnabled = False`` in ``settings`` in place,
    keeping every other key. Returns whether anything changed."""
    privacy = settings.get("privacy")
    if not isinstance(privacy, dict):
        if privacy is not None:
            logger.warning("[Gemini settings] replacing a non-object `privacy` value (%r)", type(privacy).__name__)
        privacy = {}
        settings["privacy"] = privacy
    current = privacy.get("usageStatisticsEnabled")
    if current is False:
        return False
    if current is not None:
        logger.warning(
            "[Gemini settings] overriding privacy.usageStatisticsEnabled=%r: usage statistics "
            "need an admin-level opt-in, which no agent setting can give (trinity-enterprise#986)",
            current,
        )
    privacy["usageStatisticsEnabled"] = False
    return True


def _strip_jsonc_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside strings. The CLI accepts
    them (it rejects trailing commas, so those stay an error here too)."""
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load_settings(path: Path) -> Optional[dict]:
    """The settings as a dict, ``{}`` when absent/empty, ``None`` when invalid."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = json.loads(_strip_jsonc_comments(text))
        except json.JSONDecodeError:
            return None
        logger.info("[Gemini settings] %s has comments; they are dropped on rewrite", path)
    return data if isinstance(data, dict) else None


def write_settings(path: Path, settings: dict) -> None:
    """Atomic owner-only write: a temp file in the same directory, then
    ``os.replace``. The ``.tmp`` suffix is git-ignored, so a crash-orphaned
    copy (which carries the MCP bearer token) is never committed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(settings, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_usage_statistics_disabled(path: Optional[Path] = None) -> bool:
    """Make sure the settings file carries the opt-out. Writes only when it
    drifted. Returns True when the opt-out is on disk.

    An invalid file (not JSON, not JSONC, not an object) is left untouched and
    logged: the CLI refuses to start on it, so no turn runs and no id is
    created, while overwriting it would destroy the agent's config.

    Synchronous on purpose: callers run it on the event-loop thread with no
    await before the spawn, so two turns cannot interleave a read and a write.
    """
    path = path or GEMINI_SETTINGS_PATH
    try:
        settings = load_settings(path)
        if settings is None:
            logger.error(
                "[Gemini settings] %s is not a valid settings object; left as is "
                "(the Gemini CLI will refuse to start until it is fixed)", path,
            )
            return False
        if apply_privacy_defaults(settings):
            write_settings(path, settings)
            logger.info("[Gemini settings] wrote privacy.usageStatisticsEnabled=false to %s", path)
        return True
    except Exception:
        logger.exception("[Gemini settings] could not write the usage-statistics opt-out to %s", path)
        return False
