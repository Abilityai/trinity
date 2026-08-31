"""
Public download endpoint for outbound agent file sharing (FILES-001 Step 4).

Resolves the token-scoped URLs minted by POST /api/internal/agent-files/share.
Unauthenticated — the 192-bit `sig` token IS the auth credential, minted at
share time and known only to the recipient.

Error matrix:
- 404 — file_id does not exist
- 401 — download_token missing or wrong (constant-time compare)
- 410 — revoked or expired
- 429 — IP rate limit
- 500 — storage file missing on disk
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from database import db
from services.agent_shared_files_service import STORAGE_ROOT
from services.platform_audit_service import AuditEventType, platform_audit_service
from routers.auth import get_redis_client
from routers.public import _get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])

_CHUNK_SIZE = 64 * 1024

# C5: File-download rate limit has its OWN bucket so heavy download
# traffic can't exhaust the shared public_link_lookups bucket used by
# /api/public/* endpoints. Same 60/min per IP default; configurable via
# redis key prefix.
_DOWNLOAD_RATE_LIMIT = 60         # requests per window per IP
_DOWNLOAD_RATE_WINDOW = 60        # window in seconds

# ent#461: ceiling on the cacheable window, independent of how long the link
# lives. A 30-day share should not licence a 30-day device cache.
_MAX_CACHE_SECONDS = 3600


def _check_file_download_rate_limit(client_ip: str) -> None:
    """
    Rate-limit GETs to /api/files/{id} per client IP.

    Fails open if Redis is unavailable (logs a warning) — same convention
    as public-link rate limiting. Uses a dedicated `file_downloads:{ip}`
    bucket so it can't starve other public endpoints.
    """
    r = get_redis_client()
    if r is None:
        logger.warning("File download rate limiting unavailable — Redis not connected")
        return
    key = f"file_downloads:{client_ip}"
    try:
        attempts = r.get(key)
        if attempts and int(attempts) >= _DOWNLOAD_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _DOWNLOAD_RATE_WINDOW)
        pipe.execute()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"File download rate limit check failed: {e}")


# --------------------------------------------------------------------------- #
# ent#461 — delivery policy
# --------------------------------------------------------------------------- #
#
# The bug: a `share_file` link was unopenable on mobile. The bytes were fine and
# the signature check was fine — the RESPONSE SHAPE was wrong in four ways at
# once, and the fixes below are all on the delivery layer. The auth layer is
# deliberately untouched.

#: MIME types that may be served INLINE. An allowlist, and a short one.
#:
#: This narrows a deliberate security decision rather than reversing it. The
#: original code forced `attachment` on everything with the note "defense against
#: XSS via agent-uploaded HTML", and that reasoning is still correct: this route
#: serves agent-authored bytes from `public.*`, the same origin as public chat, so
#: an inline `text/html` is stored XSS.
#:
#: What is deliberately NOT here, and why each is tempting:
#:   * text/html, application/xhtml+xml — the original threat, unchanged.
#:   * image/svg+xml — an image by name and a script host in fact. SVG carries
#:     <script> and event handlers, and browsers execute them on direct
#:     navigation. This is the one people add by reflex when they write
#:     "image/*".
#:   * everything else — an allowlist means silence is refusal.
#:
#: The type is server-detected by python-magic from the file's own bytes at share
#: time (`agent_shared_files_service.detect_mime`), never taken from the agent,
#: and it falls back to `application/octet-stream` when magic is unavailable —
#: which is not in this set, so the fallback direction is `attachment`.
_INLINE_SAFE_TYPES = frozenset({
    "audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg", "audio/webm", "audio/flac",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
    "application/pdf",
})

#: Legacy / unregistered types that players refuse, mapped to the registered
#: name. `audio/x-wav` with `nosniff` is exactly what made the reported WAV
#: unplayable: the type is not registered, so a strict player declines it and
#: `nosniff` forbids the browser from guessing something better.
_MIME_ALIASES = {
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-mpeg": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4",
    "audio/x-flac": "audio/flac",
    "video/x-matroska": "video/webm",
    "image/x-png": "image/png",
}


def normalize_mime(mime_type: Optional[str]) -> str:
    """Map a legacy/unregistered MIME to its registered name.

    Case- and parameter-insensitive: magic can return `audio/x-wav; charset=binary`.
    """
    if not mime_type:
        return "application/octet-stream"
    base = mime_type.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(base, base or "application/octet-stream")


def is_inline_safe(mime_type: Optional[str]) -> bool:
    """Whether this type may be rendered inline. Allowlist — see `_INLINE_SAFE_TYPES`."""
    return normalize_mime(mime_type) in _INLINE_SAFE_TYPES


def _iter_file(path: str, start: int = 0, length: Optional[int] = None):
    """Stream a file in chunks, optionally a byte range, without loading it.

    `length=None` streams to EOF. The final chunk is clamped so a range never
    over-reads past its end — the bug that turns a `206` into a body longer than
    its own `Content-Length`, which every client reads as a truncated download.
    """
    remaining = length
    with open(path, "rb") as fh:
        if start:
            fh.seek(start)
        while remaining is None or remaining > 0:
            size = _CHUNK_SIZE if remaining is None else min(_CHUNK_SIZE, remaining)
            chunk = fh.read(size)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def parse_range_header(value: Optional[str], file_size: int):
    """Parse a single-range `Range: bytes=…` header.

    Returns `(start, end)` inclusive, `None` when there is no usable range (the
    caller serves a normal 200), or the string ``"unsatisfiable"`` for a
    syntactically valid range that falls outside the file (416).

    ONLY single ranges. A multi-range request is answered with the full 200,
    which RFC 7233 §3.1 explicitly permits ("a server MAY ignore the Range
    header") — `multipart/byteranges` is a large amount of machinery for a case
    no media player generates.
    """
    if not value or file_size <= 0:
        return None
    value = value.strip()
    if not value.lower().startswith("bytes="):
        return None                      # only the `bytes` unit is defined
    spec = value[6:].strip()
    if "," in spec:
        return None                      # multi-range → full 200, see above
    if "-" not in spec:
        return None

    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if not first:
            # Suffix form: `bytes=-500` means the LAST 500 bytes, not "from 0".
            # Getting this backwards serves the wrong bytes with a 206, which no
            # client can detect.
            suffix = int(last)
            if suffix <= 0:
                return "unsatisfiable"
            start = max(0, file_size - suffix)
            return (start, file_size - 1)
        start = int(first)
        end = int(last) if last else file_size - 1
    except ValueError:
        return None                      # malformed → ignore, serve 200

    if start < 0 or start >= file_size:
        return "unsatisfiable"
    return (start, min(end, file_size - 1))


def _format_disposition(filename: str, *, inline: bool) -> str:
    """RFC 6266 Content-Disposition with a UTF-8 fallback.

    `inline` is decided by the caller from `is_inline_safe()` — never from
    anything the agent or the requester supplies.
    """
    disposition = "inline" if inline else "attachment"
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    safe_ascii = ascii_name.replace('"', "").replace("\\", "")
    utf8_encoded = quote(filename, safe="")
    return f'{disposition}; filename="{safe_ascii}"; filename*=UTF-8\'\'{utf8_encoded}'


def _cache_control_for(row) -> str:
    """`private` for whatever remains of the signed lifetime, capped.

    Never `public` — the URL is the credential, and a shared cache would serve
    it to whoever asked next. Never longer than the link itself lives, so a
    revoked or expired link cannot keep playing from a device cache for longer
    than it was ever valid. Falls back to `no-store` if the expiry cannot be
    read, which is the old behaviour and the safe direction.
    """
    try:
        remaining = int((_parse_expires(row["expires_at"]) - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001 — a malformed expiry is handled by the caller
        return "private, no-store"
    if remaining <= 0:
        return "private, no-store"
    return f"private, max-age={min(remaining, _MAX_CACHE_SECONDS)}"


def _parse_expires(value: str) -> datetime:
    """Parse the ISO timestamp stored in expires_at, guaranteeing tz-aware."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _validate_download_request(
    file_id: str,
    request: Request,
    sig: Optional[str],
    download_token_alias: Optional[str],
) -> tuple:
    """
    Shared validation for GET and HEAD requests.

    Returns ``(row, storage_path, headers, mime_type, client_ip)`` on success.
    Raises HTTPException on any failure (401 / 404 / 410 / 500 / 429).
    """
    client_ip = _get_client_ip(request)
    _check_file_download_rate_limit(client_ip)  # 429 on limit (C5 — dedicated bucket)

    # Accept either `sig` (preferred) or `download_token` (legacy alias)
    token = sig or download_token_alias
    if not token:
        raise HTTPException(status_code=401, detail="sig required")

    row = db.get_agent_shared_file(file_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    # Constant-time compare to prevent timing oracles
    if not secrets.compare_digest(token, row["download_token"]):
        raise HTTPException(status_code=401, detail="invalid download_token")

    if row["revoked_at"]:
        raise HTTPException(status_code=410, detail="revoked")

    try:
        expires = _parse_expires(row["expires_at"])
    except ValueError:
        logger.error("[files] malformed expires_at on file_id=%s: %r", file_id, row.get("expires_at"))
        raise HTTPException(status_code=500, detail="storage error")
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=410, detail="expired")

    storage_path = os.path.join(STORAGE_ROOT, row["stored_filename"])
    if not os.path.exists(storage_path):
        logger.error(
            "[files] orphan DB row for file_id=%s — stored_filename=%s missing on disk",
            file_id, row["stored_filename"],
        )
        raise HTTPException(status_code=500, detail="storage error")

    # ent#461: the byte length comes from DISK, not from the DB row.
    #
    # `size_bytes` was written at share time and is what the old code sent as
    # `Content-Length`. Any drift between the two is unrecoverable for the
    # client — too small truncates the download, too large hangs it waiting for
    # bytes that never come — and Range math computed against a wrong total
    # produces a `Content-Range` that contradicts the body. The file on disk is
    # the only thing actually being served, so it is the authority.
    try:
        file_size = os.path.getsize(storage_path)
    except OSError as e:
        logger.error("[files] cannot stat storage for file_id=%s: %s", file_id, e)
        raise HTTPException(status_code=500, detail="storage error")
    if file_size != (row["size_bytes"] or 0):
        logger.warning(
            "[files] size drift on file_id=%s: db=%s disk=%s — serving disk",
            file_id, row["size_bytes"], file_size,
        )

    mime_type = normalize_mime(row["mime_type"])
    inline = is_inline_safe(mime_type)

    headers = {
        "Content-Disposition": _format_disposition(row["filename"], inline=inline),
        # Kept, and load-bearing precisely BECAUSE some responses are now inline:
        # it stops a browser second-guessing the normalized type and rendering
        # something we did not classify as inline-safe.
        "X-Content-Type-Options": "nosniff",
        # ent#461: advertised on every response, including HEAD and the plain
        # 200 — a player that cannot see `accept-ranges` up front will not
        # attempt a ranged request at all, so supporting Range without
        # advertising it fixes nothing.
        "Accept-Ranges": "bytes",
        # ent#461: this link exists to be opened from Telegram, Slack or
        # WhatsApp. `same-origin` — the FastAPI/Starlette default posture for
        # this route — is what blocked every one of those from embedding or
        # previewing it. Scoped to this route; it does not change the app's
        # policy anywhere else.
        "Cross-Origin-Resource-Policy": "cross-origin",
        # ent#461: `no-store` forbade the in-app browser from buffering media to
        # disk, and iOS will not begin playback it cannot buffer. `private`
        # still keeps it out of shared caches, and the URL is unguessable and
        # already time-boxed by `expires_at`, so caching for the signed lifetime
        # discloses nothing that possessing the URL did not already.
        "Cache-Control": _cache_control_for(row),
    }
    return row, storage_path, headers, mime_type, client_ip, file_size


@router.get("/{file_id}")
async def download_shared_file(
    file_id: str,
    request: Request,
    sig: Optional[str] = None,
    download_token: Optional[str] = None,
):
    """
    Serve a file previously registered via POST /api/internal/agent-files/share.

    Query parameters:
    - sig (required): 192-bit token minted at share time, sole auth credential.

    `download_token` is accepted as a legacy alias but deprecated —
    Trinity's credential sanitizer redacts `...TOKEN...=value` query
    pairs from agent responses, stripping the token in transit. New
    URLs emit `?sig=...`.
    """
    row, storage_path, headers, mime_type, client_ip, file_size = await _validate_download_request(
        file_id, request, sig, download_token,
    )
    agent_name = row["agent_name"]

    # ent#461: Range handling. Without a 206 an iOS player will not start audio
    # at all, which is the reported bug in one line.
    rng = parse_range_header(request.headers.get("range"), file_size)

    if rng == "unsatisfiable":
        # RFC 7233 §4.4 — a 416 MUST carry the real length so the client can
        # re-ask correctly instead of retrying the same bad range forever.
        raise HTTPException(
            status_code=416,
            detail="requested range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
        )

    # A media player fetches one file as MANY ranged requests. Counting each as
    # a download would turn one play into dozens, and writing an audit row per
    # chunk would bury the log. Count and audit the request that STARTS the
    # transfer — a plain GET, or the range beginning at byte 0 — so the numbers
    # keep meaning "someone fetched this file".
    is_transfer_start = rng is None or rng[0] == 0

    if is_transfer_start:
        # Counters — best-effort
        try:
            db.mark_shared_file_downloaded(file_id)
        except Exception as e:  # pragma: no cover
            logger.warning("[files] failed to mark_downloaded for %s: %s", file_id, e)

        # Audit — best-effort
        try:
            await platform_audit_service.log(
                event_type=AuditEventType.EXECUTION,
                event_action="file_share_download",
                source="public",
                actor_ip=client_ip,
                target_type="agent",
                target_id=agent_name,
                details={
                    "file_id": file_id,
                    "filename": row["filename"],
                    "size_bytes": file_size,
                    "mime_type": mime_type,
                    "ranged": rng is not None,
                    "user_agent": (request.headers.get("user-agent") or "")[:200],
                },
                endpoint=str(request.url.path),
            )
        except Exception as e:  # pragma: no cover
            logger.warning("[files] audit log failed for %s: %s", file_id, e)

    if rng is not None:
        start, end = rng
        length = end - start + 1
        return StreamingResponse(
            _iter_file(storage_path, start, length),
            status_code=206,
            media_type=mime_type,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
            },
        )

    return StreamingResponse(
        _iter_file(storage_path),
        media_type=mime_type,
        headers={**headers, "Content-Length": str(file_size)},
    )


@router.head("/{file_id}")
async def head_shared_file(
    file_id: str,
    request: Request,
    sig: Optional[str] = None,
    download_token: Optional[str] = None,
):
    """
    HEAD handler for link previewers / CDNs that probe before GET.

    Runs the same validation as GET (rate limit, token, expiry, revoke,
    storage presence) and returns the same headers — but no body, no
    download counter bump, no audit row. Follows RFC 7231 §4.3.2:
    HEAD is identical to GET except the server MUST NOT return a
    message-body.
    """
    _row, _storage_path, headers, mime_type, _client_ip, file_size = await _validate_download_request(
        file_id, request, sig, download_token,
    )
    # ent#461: HEAD must carry `Accept-Ranges` and the true `Content-Length` —
    # it is the probe a player makes BEFORE deciding whether it can stream, so a
    # HEAD that omits them defeats Range support even though GET implements it.
    return Response(
        status_code=200,
        headers={**headers, "Content-Length": str(file_size)},
        media_type=mime_type,
    )
