"""
Share-creation service for outbound file sharing (amazing-file-outbound, FILES-001 Step 3).

Responsibilities:
- Validate the filename the agent names via MCP (reject absolute, `..`, etc.)
- Extract the single file via Docker SDK `get_archive`
- Enforce size cap + per-agent quota
- Detect MIME with python-magic and reject executables
- Persist to /data/agent-files/{file_id} (under the existing trinity-data mount)
- Insert into agent_shared_files
- Return {file_id, url, expires_at, size_bytes, mime_type, one_time}
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import secrets
import shutil
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Optional

import docker.errors
from fastapi import HTTPException

from database import db
from services import idempotency_service
from services.docker_service import get_agent_container
from services.docker_utils import container_get_archive
from services.settings_service import get_public_chat_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — MVP hardcoded; can migrate to settings later
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024          # 50 MB per file
MAX_AGENT_QUOTA_BYTES = 500 * 1024 * 1024       # 500 MB per agent
MIN_EXPIRES_IN = 60                             # 1 minute
MAX_EXPIRES_IN = 7 * 24 * 60 * 60               # 7 days
DEFAULT_EXPIRES_IN = MAX_EXPIRES_IN

# C3: refuse writes when /data has less than this much free space.
# 500 MB × (typical concurrent shares + SQLite WAL + Vector logs) is a
# reasonable floor before the shared `/data` mount starts causing
# problems for the backend itself (DB writes, Vector, log archives).
MIN_FREE_DISK_BYTES = 500 * 1024 * 1024

PUBLISH_DIR = "/home/developer/public"
STORAGE_ROOT = "/data/agent-files"

# Magic-byte signatures for executables we reject outright.
EXECUTABLE_SIGNATURES = (
    b"MZ",                      # PE (Windows)
    b"\x7fELF",                 # ELF (Linux)
    b"\xca\xfe\xba\xbe",        # Mach-O (32 & fat binaries)
    b"\xcf\xfa\xed\xfe",        # Mach-O (64 little-endian)
    b"\xfe\xed\xfa\xce",        # Mach-O (32 big-endian)
    b"\xfe\xed\xfa\xcf",        # Mach-O (64 big-endian)
    b"#!",                      # Shell/interpreter scripts
)

# Optional python-magic; gracefully fall back to None on import failure
# (docker/backend/Dockerfile installs libmagic1 + python-magic since #354).
try:  # pragma: no cover
    import magic  # type: ignore

    _MAGIC_AVAILABLE = True
except Exception:  # pragma: no cover
    _MAGIC_AVAILABLE = False
    logger.warning("[shared-files] python-magic unavailable; MIME detection falls back to 'application/octet-stream'")


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def validate_publish_path(filename: str) -> str:
    """
    Ensure the caller-provided filename is a safe relative path inside
    PUBLISH_DIR. Returns the **container-absolute** path on success.

    Raises HTTPException(400, 'PATH_TRAVERSAL') for any escape attempt.
    """
    if not filename:
        raise HTTPException(status_code=400, detail="PATH_TRAVERSAL: filename required")

    # Reject absolute paths — agent should only name things relative to the
    # publish dir.
    if filename.startswith("/"):
        raise HTTPException(status_code=400, detail="PATH_TRAVERSAL: absolute paths rejected")

    # PurePosixPath normalises `./a/b` → `a/b` and reveals escapes as `..`.
    parts = PurePosixPath(filename).parts
    if any(p in ("..", "") for p in parts):
        raise HTTPException(status_code=400, detail="PATH_TRAVERSAL: '..' segments rejected")
    if any(p.startswith("/") for p in parts):
        raise HTTPException(status_code=400, detail="PATH_TRAVERSAL: absolute component rejected")

    # Rebuild under the publish dir; guard against backslash tricks on
    # Windows-style input (defense in depth even though containers are linux).
    if "\\" in filename:
        raise HTTPException(status_code=400, detail="PATH_TRAVERSAL: backslashes rejected")

    resolved = str(PurePosixPath(PUBLISH_DIR, *parts))
    return resolved


# ---------------------------------------------------------------------------
# MIME detection + blocklist
# ---------------------------------------------------------------------------


def detect_mime(data: bytes) -> str:
    """Return the MIME type inferred from the first bytes of `data`."""
    if _MAGIC_AVAILABLE:
        try:
            return magic.from_buffer(data[:4096], mime=True) or "application/octet-stream"
        except Exception as e:
            logger.warning(f"[shared-files] MIME detection failed: {e}")
    return "application/octet-stream"


def check_mime_blocklist(data: bytes, mime_type: str) -> None:
    """Raise HTTPException(400) if `data`/`mime_type` looks like an executable."""
    # Header-byte check — strongest signal
    for sig in EXECUTABLE_SIGNATURES:
        if data.startswith(sig):
            raise HTTPException(status_code=400, detail=f"MIME_BLOCKED: executable content ({sig!r})")

    # MIME-type check as backup
    blocked_prefixes = ("application/x-executable", "application/x-dosexec", "application/x-mach-binary")
    if any(mime_type.startswith(p) for p in blocked_prefixes):
        raise HTTPException(status_code=400, detail=f"MIME_BLOCKED: {mime_type}")


# ---------------------------------------------------------------------------
# Quota + extraction
# ---------------------------------------------------------------------------


def enforce_quota(agent_name: str, new_bytes: int) -> None:
    """Raise HTTPException(413) if the new file would exceed the agent's quota."""
    current = db.total_shared_file_bytes_for_agent(agent_name)
    if current + new_bytes > MAX_AGENT_QUOTA_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"QUOTA_EXCEEDED: {current + new_bytes} bytes would exceed {MAX_AGENT_QUOTA_BYTES}",
        )


async def extract_from_agent(
    agent_name: str, container_path: str
) -> tuple[bytes, str]:
    """
    Pull the single file at `container_path` out of the agent container.

    Returns (raw_bytes, basename). Raises:
    - 404 FILE_NOT_FOUND     if the path doesn't exist in the container
    - 400 NOT_REGULAR_FILE   if the entry is a dir, symlink, or device
    - 413 SIZE_LIMIT_EXCEEDED if raw size > MAX_FILE_SIZE_BYTES
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        stream, _stat = await container_get_archive(container, container_path)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"FILE_NOT_FOUND: {container_path}")

    # `stream` is a generator of tar bytes — join it, then untar in memory.
    # Cap the buffer early at MAX_FILE_SIZE_BYTES + tar overhead (~1 KB) so a
    # malicious agent can't OOM us by naming a huge file.
    cap = MAX_FILE_SIZE_BYTES + 4096
    buf = bytearray()
    for chunk in stream:
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(status_code=413, detail="SIZE_LIMIT_EXCEEDED")

    try:
        with tarfile.open(fileobj=io.BytesIO(bytes(buf)), mode="r") as tar:
            members = [m for m in tar.getmembers() if m.name]
            if not members:
                raise HTTPException(status_code=404, detail="FILE_NOT_FOUND: empty archive")
            member = members[0]
            if not member.isfile():
                raise HTTPException(status_code=400, detail="NOT_REGULAR_FILE")
            if member.size > MAX_FILE_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="SIZE_LIMIT_EXCEEDED")

            extracted = tar.extractfile(member)
            if extracted is None:
                raise HTTPException(status_code=400, detail="NOT_REGULAR_FILE")
            data = extracted.read()
            return data, os.path.basename(member.name)
    except tarfile.TarError as e:
        raise HTTPException(status_code=500, detail=f"archive extraction failed: {e}")


# ---------------------------------------------------------------------------
# Filename sanitization for download headers (Content-Disposition)
# ---------------------------------------------------------------------------

# Conservative allowlist for on-disk + display filenames. Anything outside
# becomes '_'. Prevents CRLF injection into Content-Disposition and keeps
# filesystems happy.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]")


def sanitize_display_name(name: str, fallback: str) -> str:
    if not name:
        return fallback
    cleaned = _SAFE_NAME_RE.sub("_", name.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


# ---------------------------------------------------------------------------
# URL building — matches public-chat convention
# ---------------------------------------------------------------------------


def build_download_url(file_id: str, download_token: str) -> str:
    """
    Build the external download URL.

    Uses the `/api/files/{id}` path so requests traverse the Vite dev
    proxy and the prod nginx config's existing `/api/*` rules — no
    dedicated `/files/*` proxy needed anywhere.

    The query parameter name is `sig` (signature) rather than
    `download_token`. The backend's credential sanitizer
    (`utils/credential_sanitizer.py`) redacts any `...TOKEN...=value`
    pattern, which would strip our legitimate token out of agent
    responses before they reach the user. `sig` avoids all the
    sanitizer's sensitive-key patterns.

    If `public_chat_url` is configured, the URL is absolute and
    user-shareable; otherwise it's relative (frontend resolves vs.
    window.origin on click).
    """
    base = (get_public_chat_url() or "").rstrip("/")
    path = f"/api/files/{file_id}?sig={download_token}"
    return f"{base}{path}" if base else path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _ensure_storage_dir() -> str:
    os.makedirs(STORAGE_ROOT, exist_ok=True)
    return STORAGE_ROOT


def check_disk_space(needed_bytes: int, *, root: str = "/data", min_free: int = MIN_FREE_DISK_BYTES) -> None:
    """
    Refuse to write if `root` doesn't have `needed_bytes + min_free` free.

    Raises HTTPException(507 Insufficient Storage) on failure. This runs
    before every share to protect the shared `/data` mount — the same
    filesystem holds the SQLite DB, Vector logs, and log archives, so
    letting an agent fill it causes platform-wide outage, not just a
    failed share.
    """
    try:
        usage = shutil.disk_usage(root)
    except Exception as e:
        logger.warning(f"[shared-files] disk_usage({root}) failed: {e} — skipping check")
        return
    required = needed_bytes + min_free
    if usage.free < required:
        logger.error(
            "[shared-files] refusing write: /data has %d free bytes, "
            "need %d (file=%d + floor=%d)",
            usage.free, required, needed_bytes, min_free,
        )
        raise HTTPException(
            status_code=507,
            detail=(
                f"INSUFFICIENT_STORAGE: platform disk is too full to accept "
                f"a {needed_bytes}-byte share (need {min_free} bytes free after write, "
                f"have {usage.free})."
            ),
        )


def _validate_expires_in(expires_in: Optional[int]) -> int:
    """Normalise + bounds-check the requested share lifetime."""
    if expires_in is None:
        expires_in = DEFAULT_EXPIRES_IN
    if expires_in < MIN_EXPIRES_IN or expires_in > MAX_EXPIRES_IN:
        raise HTTPException(
            status_code=400,
            detail=f"INVALID_EXPIRATION: expires_in must be between {MIN_EXPIRES_IN} and {MAX_EXPIRES_IN}",
        )
    return expires_in


def _persist_and_register(
    agent_name: str,
    data: bytes,
    *,
    basename: str,
    display_name: Optional[str],
    expires_in: int,
    created_by: Optional[str],
) -> dict:
    """
    Shared tail of the share pipeline: MIME-detect → blocklist → quota →
    disk-check → persist to disk → DB insert → mint URL.

    Operates on in-memory bytes that have *already* been obtained. The two
    public entry points differ only in where ``data`` comes from:
    ``create_share`` extracts it from the agent container; ``create_share_from_bytes``
    receives it from the caller (e.g. outbound channel media, #1315). Keeping the
    DB-failure cleanup (``os.unlink``) here means both paths get it for free.
    """
    size_bytes = len(data)

    # --- MIME ---
    mime_type = detect_mime(data)
    check_mime_blocklist(data, mime_type)

    # --- quota ---
    enforce_quota(agent_name, size_bytes)

    # --- disk-space pre-check (C3) ---
    _ensure_storage_dir()
    check_disk_space(size_bytes)

    # --- persist bytes on disk ---
    file_id = str(uuid.uuid4())
    stored_filename = file_id  # UUID filename — name is opaque on disk
    stored_path = os.path.join(STORAGE_ROOT, stored_filename)
    with open(stored_path, "wb") as fh:
        fh.write(data)

    # --- DB row ---
    display = sanitize_display_name(display_name or basename, fallback=f"file-{file_id}.bin")
    download_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)

    try:
        db.create_agent_shared_file(
            file_id=file_id,
            agent_name=agent_name,
            filename=display,
            stored_filename=stored_filename,
            size_bytes=size_bytes,
            mime_type=mime_type,
            download_token=download_token,
            created_by=created_by or agent_name,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
        )
    except Exception:
        # Don't leak half-written files on DB failure
        try:
            os.unlink(stored_path)
        except Exception:
            pass
        raise

    url = build_download_url(file_id, download_token)
    logger.info(
        "[shared-files] agent=%s shared file_id=%s filename=%s size=%d mime=%s",
        agent_name, file_id, display, size_bytes, mime_type,
    )

    return {
        "file_id": file_id,
        "url": url,
        "expires_at": expires_at.isoformat(),
        "size_bytes": size_bytes,
        "mime_type": mime_type,
    }


async def create_share(
    agent_name: str,
    filename: str,
    *,
    display_name: Optional[str] = None,
    expires_in: Optional[int] = None,
    created_by: Optional[str] = None,
    execution_id: Optional[str] = None,
    dedup_label: str = "",
) -> dict:
    """
    End-to-end: extract → validate → persist → DB insert → return URL payload.

    Effect-scoped idempotency (#1084): the token-mint + persist is wrapped in
    the effect guard keyed on the filename + a sha256 of the extracted CONTENT,
    scoped to ``execution_id``. A re-run of the same turn sharing the same file
    replays the original signed URL instead of minting a second token; a changed
    file (different content) under the same name produces a new share. Fail-open
    when ``execution_id`` is absent/invalid (old image / the internal path).
    """
    # --- flag gate ---
    if not db.get_file_sharing_enabled(agent_name):
        raise HTTPException(status_code=403, detail="FEATURE_DISABLED: file sharing is not enabled for this agent")

    # --- expiration ---
    expires_in = _validate_expires_in(expires_in)

    # --- path ---
    container_path = validate_publish_path(filename)

    # --- extract (needed up-front so the effect key can version on content) ---
    data, basename = await extract_from_agent(agent_name, container_path)
    content_version = hashlib.sha256(data).hexdigest()

    async with idempotency_service.effect_guard(
        "share_file",
        {"filename": filename, "content": content_version},
        execution_id=execution_id,
        agent_name=agent_name,
        dedup_label=dedup_label,
    ) as guard:
        if guard.replay:
            # A completed replay is terminal — never re-mint the share. If the
            # stored snapshot is missing (NULL/unparseable in the row — unreachable
            # in normal flow, but the DB contract permits it) we cannot rebuild the
            # signed URL, so surface an error rather than minting a second token (#1084 I2).
            if guard.snapshot is None:
                raise HTTPException(
                    status_code=409,
                    detail="Share already created for this execution but its URL is unavailable.",
                )
            return guard.snapshot
        result = _persist_and_register(
            agent_name,
            data,
            basename=basename,
            display_name=display_name,
            expires_in=expires_in,
            created_by=created_by,
        )
        guard.snapshot = result
        return result


def create_share_from_bytes(
    agent_name: str,
    data: bytes,
    *,
    display_name: str,
    expires_in: Optional[int] = None,
    created_by: Optional[str] = None,
    require_sharing_enabled: bool = True,
) -> dict:
    """
    Persist already-in-memory bytes as a public share and mint a download URL —
    the outbound-channel-media counterpart to ``create_share`` (#1315).

    Same MIME-blocklist / quota / disk / DB pipeline as ``create_share``; the
    bytes come from the caller (e.g. ``ChannelResponse.files`` artifacts) rather
    than being extracted from the agent container. Used by channels like WhatsApp
    that deliver media by URL (Twilio fetches the public ``?sig=`` URL
    server-side) instead of uploading bytes directly.

    ``require_sharing_enabled=False`` skips the per-agent file-sharing toggle for
    internally-generated media that has its OWN feature gate — e.g. WhatsApp voice
    replies (trinity-enterprise#56), which must host a transient audio note for
    Twilio to fetch and are already gated by ``tts_voice_replies_enabled``. The
    MIME-blocklist / quota / disk protections still apply.

    Raises ``HTTPException`` (403 gate / 400 expiry / 413 quota / 507 disk /
    400 MIME-blocked) exactly like ``create_share`` — callers must isolate it so
    one rejected file never aborts a whole response.
    """
    if require_sharing_enabled and not db.get_file_sharing_enabled(agent_name):
        raise HTTPException(status_code=403, detail="FEATURE_DISABLED: file sharing is not enabled for this agent")

    expires_in = _validate_expires_in(expires_in)

    return _persist_and_register(
        agent_name,
        data,
        basename=display_name,
        display_name=display_name,
        expires_in=expires_in,
        created_by=created_by,
    )
