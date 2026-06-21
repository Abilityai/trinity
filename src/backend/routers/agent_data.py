"""
Agent runtime-data export / import (#1169).

On-demand snapshot/export and restore of an agent's declared runtime data
(`/home/developer/data`) over the EXISTING durable home volume — no separate
volume, no platform schema change. The home directory is already a persistent
named Docker volume, so this router adds only the portability surface the issue
was really missing:

- ``POST /api/agents/{name}/data/export`` — stream a tar of the agent's
  ``data/`` directory to the caller. Captured via ``get_archive`` (the agent
  workspace is never mounted), streamed through a temp file under ``/data`` so a
  multi-GB dataset never lands in memory, capped at ``AGENT_DATA_EXPORT_MAX_BYTES``
  (413 on overflow). The tar embeds a self-describing ``manifest.json``.
- ``POST /api/agents/{name}/data/import`` — restore an uploaded tar into the
  agent's ``data/`` via the existing agent-server ``/api/agent-server/restore``
  primitive, whose ``restore_from_tar`` enforces the ``data/**`` allowlist and
  rejects ``..`` / absolute-path traversal.

Both are owner/admin only and serialized per agent by a cross-worker Redis
operation lock (409 on contention). Three-layer split (Invariant #1):
business logic stays thin here because the heavy lifting reuses
``docker_utils`` (export transport) and the agent-server restore primitive.
"""
import asyncio
import base64
import io
import json
import logging
import os
import tarfile
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import docker
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from dependencies import OwnedAgentByName, get_current_user
from models import AgentDataImportResponse, User
from services import git_service, idempotency_service
from services.agent_service.helpers import agent_http_request
from services.docker_service import get_agent_container
from services.docker_utils import container_get_archive
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-data"])

# Container path of the agent runtime-data root (#1169). Always a single root
# so the backend can snapshot it cleanly with one `get_archive`.
_DATA_CONTAINER_PATH = "/home/developer/data"

# Default 5 GiB export ceiling; operator-tunable. Guards the platform disk and
# the event loop — a runaway dataset returns 413 instead of filling `/data`.
AGENT_DATA_EXPORT_MAX_BYTES = int(
    os.getenv("AGENT_DATA_EXPORT_MAX_BYTES", str(5 * 1024 * 1024 * 1024))
)

# Inline (base64) export ceiling for the MCP portability surface — small
# runtime data (e.g. a few-MB SQLite DB) round-trips through MCP's text
# transport; anything larger must use the streaming download. Default 10 MiB.
AGENT_DATA_INLINE_MAX_BYTES = int(
    os.getenv("AGENT_DATA_INLINE_MAX_BYTES", str(10 * 1024 * 1024))
)

# TTL ceiling on the per-agent op lock — longer than any plausible export so a
# crashed worker can't wedge the agent's data ops forever.
_OP_LOCK_TTL_S = int(os.getenv("AGENT_DATA_OP_LOCK_TTL", "1800"))

# Temp staging under the existing trinity-data mount (writable, same disk the
# DB lives on). The export tar is built here, streamed out, then deleted.
_TMP_DIR = "/data/agent-data-tmp"

_STREAM_CHUNK = 1024 * 1024  # 1 MiB read chunks when streaming the temp tar back


class _ExportTooLarge(Exception):
    """Raised by the drain worker when the agent data exceeds the cap."""


# ---------------------------------------------------------------------------
# Per-agent cross-worker operation lock (Redis SETNX, fail-open)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _agent_data_op_lock(agent_name: str):
    """Serialize export/import per agent across uvicorn workers.

    Redis SETNX with a TTL backstop; fail-open if Redis is unavailable (a flaky
    lock layer must never block a real operation). 409 on contention.
    """
    from routers.auth import get_redis_client

    key = f"agent:data_op:{agent_name}"
    token = uuid.uuid4().hex
    client = None
    held = False
    try:
        client = get_redis_client()
    except Exception:  # pragma: no cover - defensive
        client = None

    if client is not None:
        try:
            held = bool(client.set(key, token, nx=True, ex=_OP_LOCK_TTL_S))
        except Exception:
            # Fail-open: proceed without the lock rather than block on Redis.
            client = None
            held = False
        else:
            if not held:
                raise HTTPException(
                    status_code=409,
                    detail="A data export/import is already in progress for this agent.",
                )
    try:
        yield
    finally:
        if client is not None and held:
            try:
                # Best-effort ownership check before release; TTL backstops.
                if client.get(key) == token:
                    client.delete(key)
            except Exception:  # pragma: no cover - defensive
                logger.debug("data-op lock release failed for %s", agent_name, exc_info=True)


# ---------------------------------------------------------------------------
# Export helpers (blocking — run in the default thread executor)
# ---------------------------------------------------------------------------


def _drain_stream_to_file(stream, out_path: str, max_bytes: int) -> int:
    """Write tar chunks to `out_path`, enforcing the byte cap. Returns size."""
    total = 0
    with open(out_path, "wb") as out:
        for chunk in stream:
            total += len(chunk)
            if total > max_bytes:
                raise _ExportTooLarge(total)
            out.write(chunk)
    return total


def _append_manifest(tar_path: str, manifest: dict) -> None:
    """Embed a self-describing `manifest.json` in the export tar.

    Appends to the captured data tar, or writes a fresh manifest-only tar when
    there was no `data/` directory (the temp file is still empty — `tarfile`
    append mode raises `ReadError` on a 0-byte file). Best-effort: a manifest
    failure must not fail the export (restore ignores non-`data/**` entries).
    """
    try:
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(payload)
        info.mtime = int(time.time())
        # Empty temp file (no data/ captured) → fresh archive; else append.
        mode: Literal["a", "w"] = "a" if os.path.getsize(tar_path) > 0 else "w"
        with tarfile.open(tar_path, mode) as tf:
            tf.addfile(info, io.BytesIO(payload))
    except Exception:  # pragma: no cover - defensive
        logger.warning("Failed to embed manifest in export tar for %s", tar_path, exc_info=True)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.debug("temp export cleanup failed: %s", path, exc_info=True)


def _iter_file(path: str):
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_STREAM_CHUNK)
            if not chunk:
                break
            yield chunk


async def _drain_upload_to_file(
    upload: UploadFile, out_path: str, max_bytes: int
) -> int:
    """Stream an UploadFile to `out_path` in chunks, enforcing the byte cap.

    The import-side mirror of `_drain_stream_to_file`: the body is written to
    disk one chunk at a time and rejected (`_ExportTooLarge`) the moment it
    crosses the cap, so an oversized upload is never materialized as one large
    in-memory `bytes` (the M1 OOM guard, #1169). Returns the bytes written.
    """
    total = 0
    with open(out_path, "wb") as out:
        while True:
            chunk = await upload.read(_STREAM_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _ExportTooLarge(total)
            out.write(chunk)
    return total


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.post("/{agent_name}/data/export")
async def export_agent_data(
    request: Request,
    agent_name: OwnedAgentByName,
    current_user: User = Depends(get_current_user),
    format: str = Query(
        "stream",
        description="`stream` (binary tar download) or `base64` (small-data inline JSON for MCP).",
    ),
    idempotency_key: Optional[str] = Header(None),
):
    """Export a tar of the agent's `data/` directory (owner/admin only).

    `format=stream` (default) returns the binary tar as a download.
    `format=base64` returns a JSON envelope with the tar inline (base64) for the
    MCP portability surface, but only up to `AGENT_DATA_INLINE_MAX_BYTES`
    (413 otherwise — use the streaming download for large data).

    `Idempotency-Key` is accepted for API-contract consistency but export is a
    naturally-idempotent read — no execution is created, so it is not deduped
    through the snapshot-replay layer (which can't replay a binary stream).
    """
    if format not in ("stream", "base64"):
        raise HTTPException(
            status_code=400, detail="format must be 'stream' or 'base64'"
        )
    container = get_agent_container(agent_name)
    if container is None or container.status != "running":
        raise HTTPException(
            status_code=409,
            detail="Agent container is not running; start the agent before exporting its data.",
        )

    loop = asyncio.get_event_loop()

    declared_paths: list[str] = []
    try:
        declared_paths = await git_service._data_paths_for(agent_name)
    except Exception:  # best-effort — manifest metadata only
        declared_paths = []

    async with _agent_data_op_lock(agent_name):
        # Stage the temp file only after the op lock is held — a 409 on lock
        # contention must not leak a 0-byte temp under _TMP_DIR (#1169).
        os.makedirs(_TMP_DIR, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=_TMP_DIR, suffix=".tar")
        os.close(fd)
        try:
            try:
                stream, _stat = await container_get_archive(container, _DATA_CONTAINER_PATH)
            except docker.errors.NotFound:
                # No data/ directory yet (declared but unused) → manifest-only
                # export. A valid, importable (no-op) tar rather than a 500.
                stream = None

            if stream is not None:
                await loop.run_in_executor(
                    None,
                    _drain_stream_to_file,
                    stream,
                    tmp_path,
                    AGENT_DATA_EXPORT_MAX_BYTES,
                )

            manifest = {
                "format_version": 1,
                "agent_name": agent_name,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "data_root": "data/",
                "data_paths": declared_paths,
                "trinity_version": os.getenv("GIT_COMMIT_SHORT", "unknown"),
            }
            await loop.run_in_executor(None, _append_manifest, tmp_path, manifest)
        except _ExportTooLarge:
            _safe_unlink(tmp_path)
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Agent data exceeds the export cap of "
                    f"{AGENT_DATA_EXPORT_MAX_BYTES} bytes."
                ),
            )
        except HTTPException:
            _safe_unlink(tmp_path)
            raise
        except Exception as exc:
            _safe_unlink(tmp_path)
            logger.exception("data export failed for %s", agent_name)
            raise HTTPException(status_code=500, detail=f"Data export failed: {exc}")

    # Capture complete (lock released). Audit, then stream the temp file out and
    # delete it once the response is fully sent.
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_LIFECYCLE,
            event_action="data_export",
            source="mcp" if request.headers.get("X-Via-MCP") else "api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "size_bytes": os.path.getsize(tmp_path),
                "data_paths": declared_paths,
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.warning("[data] export audit log failed for %s", agent_name, exc_info=True)

    if format == "base64":
        size = os.path.getsize(tmp_path)
        if size > AGENT_DATA_INLINE_MAX_BYTES:
            _safe_unlink(tmp_path)
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Data ({size} bytes) exceeds the inline base64 cap of "
                    f"{AGENT_DATA_INLINE_MAX_BYTES} bytes; use the streaming "
                    f"download (format=stream)."
                ),
            )
        try:
            with open(tmp_path, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("ascii")
        finally:
            _safe_unlink(tmp_path)
        return JSONResponse(
            content={
                "agent_name": agent_name,
                "size_bytes": size,
                "format": "base64",
                "filename": f"{agent_name}-data.tar",
                "tar_base64": encoded,
            }
        )

    return StreamingResponse(
        _iter_file(tmp_path),
        media_type="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{agent_name}-data.tar"',
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(_safe_unlink, tmp_path),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@router.post("/{agent_name}/data/import", response_model=AgentDataImportResponse)
async def import_agent_data(
    request: Request,
    agent_name: OwnedAgentByName,
    tarball: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None),
):
    """Restore an uploaded tar into the agent's `data/` (owner/admin only).

    Delegates to the agent-server restore primitive, which enforces the
    `data/**` allowlist and rejects path traversal. Deduped per agent via
    `Idempotency-Key` (Invariant #18).
    """
    container = get_agent_container(agent_name)
    if container is None or container.status != "running":
        raise HTTPException(
            status_code=409,
            detail="Agent container is not running; start the agent before importing data.",
        )

    # Stream the upload to a capped temp file instead of buffering the whole
    # body in memory: an oversized upload is rejected mid-stream (413) rather
    # than OOMing the backend, and httpx then streams the staged file to the
    # agent restore primitive from a handle (M1, #1169). The cap stays
    # symmetric with the export ceiling so any exported tar round-trips back —
    # lowering it would reject a legitimately-exported dataset.
    declared_len = request.headers.get("content-length")
    if (
        declared_len
        and declared_len.isdigit()
        and int(declared_len) > AGENT_DATA_EXPORT_MAX_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail=(
                f"Upload exceeds the data size cap of "
                f"{AGENT_DATA_EXPORT_MAX_BYTES} bytes."
            ),
        )

    os.makedirs(_TMP_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=_TMP_DIR, suffix=".tar")
    os.close(fd)
    try:
        try:
            bytes_received = await _drain_upload_to_file(
                tarball, tmp_path, AGENT_DATA_EXPORT_MAX_BYTES
            )
        except _ExportTooLarge:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload exceeds the data size cap of "
                    f"{AGENT_DATA_EXPORT_MAX_BYTES} bytes."
                ),
            )

        scope = idempotency_service.make_agent_scope(agent_name)
        decision = idempotency_service.begin(scope, idempotency_key)
        if decision.replay:
            if decision.in_flight:
                raise HTTPException(
                    status_code=409,
                    detail="An import with this Idempotency-Key is still being processed.",
                )
            if decision.snapshot is not None:
                return JSONResponse(
                    content=decision.snapshot,
                    headers={"X-Idempotent-Replay": "true"},
                )

        try:
            async with _agent_data_op_lock(agent_name):
                # The handle stays open across the await so httpx streams the
                # file rather than re-buffering it. `agent_http_request` only
                # retries on ConnectError (pre-body), so the read position is
                # never consumed before a retry.
                with open(tmp_path, "rb") as fh:
                    resp = await agent_http_request(
                        agent_name,
                        "POST",
                        "/api/agent-server/restore",
                        timeout=300.0,
                        files={"tarball": ("data.tar", fh, "application/x-tar")},
                        data={"paths": json.dumps(["data/**"])},
                    )
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Agent restore failed ({resp.status_code}): {resp.text[:500]}",
                    )
                result = resp.json()
            # `manifest.json` is our own tar-root artifact, outside the data/**
            # allowlist by design, so restore always reports it skipped. Drop it
            # from the surfaced list so a caller treating non-empty `skipped` as
            # a problem isn't misled (#1169).
            skipped = [
                s
                for s in result.get("skipped_outside_allowlist", [])
                if s != "manifest.json"
            ]
            response = AgentDataImportResponse(
                agent_name=agent_name,
                restored=result.get("restored", []),
                skipped=skipped,
                bytes_received=bytes_received,
            )
        except HTTPException:
            idempotency_service.fail(decision)
            raise
        except Exception as exc:
            idempotency_service.fail(decision)
            logger.exception("data import failed for %s", agent_name)
            raise HTTPException(status_code=502, detail=f"Data import failed: {exc}")

        idempotency_service.complete(decision, None, response.model_dump())
    finally:
        _safe_unlink(tmp_path)

    try:
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_LIFECYCLE,
            event_action="data_import",
            source="mcp" if request.headers.get("X-Via-MCP") else "api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "bytes_received": bytes_received,
                "restored_count": len(response.restored),
                "skipped_count": len(response.skipped),
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.warning("[data] import audit log failed for %s", agent_name, exc_info=True)

    return response
