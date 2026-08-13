"""
Shared file upload processing for web chat and channel adapters.

Extracted from adapters/message_router.py so that both the channel adapter path
(Telegram/Slack/WhatsApp) and the web chat path (/task, /api/public/chat) can
share the same validation, MIME-checking, and container-write logic.
"""

import base64
import io
import logging
import os
import re
import tarfile
import unicodedata
from typing import List, Optional, Tuple

from services.docker_utils import container_put_archive, container_exec_run
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

# Try to import python-magic for MIME validation; graceful fallback if unavailable
try:
    import magic
    _MAGIC_AVAILABLE = True
except ImportError:
    _MAGIC_AVAILABLE = False
    logger.warning("[UPLOAD] python-magic not installed; MIME validation will trust declared MIME")

# ---------------------------------------------------------------------------
# Limits — channel adapter uses the larger set; web uses WEB_* constants
# ---------------------------------------------------------------------------

CHANNEL_MAX_FILE_SIZE = 10 * 1024 * 1024        # 10 MB per non-image file
CHANNEL_MAX_IMAGE_SIZE = 5 * 1024 * 1024         # 5 MB per image
CHANNEL_MAX_TOTAL_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB total images
CHANNEL_MAX_FILES = 10

WEB_MAX_FILE_SIZE = 5 * 1024 * 1024              # 5 MB per non-image file
WEB_MAX_IMAGE_SIZE = 5 * 1024 * 1024             # 5 MB per image
WEB_MAX_TOTAL_IMAGE_SIZE = 10 * 1024 * 1024      # 10 MB total images
WEB_MAX_FILES = 3

UNSUPPORTED_MIMES = {
    "application/pdf", "application/x-tar",
    "application/gzip", "application/x-rar-compressed",
    "video/", "audio/",
}

UPLOAD_BASE = "/home/developer/uploads"

# Filename sanitization constants (same rules as the original message_router)
_FILENAME_MAX_LENGTH = 200
_FILENAME_SAFE_CHARS_RE = re.compile(r'[^\w.\-()]')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, file_id: str, used_names: set) -> str:
    """
    Sanitize a user-supplied filename for safe placement in the agent workspace.

    Steps: NFKC normalize → basename → strip unsafe chars → truncate → dedup.
    Rejects hidden filenames (.env, .gitignore, …) to preserve security posture.
    """
    normalized = unicodedata.normalize("NFKC", name or "")
    base = os.path.basename(normalized)
    safe = _FILENAME_SAFE_CHARS_RE.sub('_', base)

    stripped = safe.strip('._')
    if not stripped or safe.startswith('.'):
        safe = f"file_{file_id}"

    if len(safe) > _FILENAME_MAX_LENGTH:
        stem, dot, ext = safe.rpartition('.')
        if dot and len(ext) <= 16:
            keep = _FILENAME_MAX_LENGTH - len(ext) - 1
            safe = f"{stem[:keep]}.{ext}"
        else:
            safe = safe[:_FILENAME_MAX_LENGTH]

    if safe in used_names:
        stem, dot, ext = safe.rpartition('.')
        if not dot:
            stem, ext = safe, ""
        suffix_n = 1
        while True:
            suffix = f"-{suffix_n}"
            candidate_stem = stem
            max_stem = _FILENAME_MAX_LENGTH - len(suffix) - (len(ext) + 1 if ext else 0)
            if len(candidate_stem) > max_stem:
                candidate_stem = candidate_stem[:max_stem]
            candidate = f"{candidate_stem}{suffix}.{ext}" if ext else f"{candidate_stem}{suffix}"
            if candidate not in used_names:
                safe = candidate
                break
            suffix_n += 1

    return safe


def format_file_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Core upload processor
# ---------------------------------------------------------------------------

async def process_file_uploads(
    raw_files: List[dict],
    agent_name: str,
    container,
    session_id: str,
    uploader: str,
    source: str = "web",
    sender_id: str = "",
    channel_id: str = "",
    max_files: int = CHANNEL_MAX_FILES,
    max_file_size: int = CHANNEL_MAX_FILE_SIZE,
    max_image_size: int = CHANNEL_MAX_IMAGE_SIZE,
    max_total_image_size: int = CHANNEL_MAX_TOTAL_IMAGE_SIZE,
) -> Tuple[List[str], Optional[str], bool, List[dict]]:
    """
    Validate and store uploaded files for an agent.

    Each entry in raw_files must have:
      - name: str
      - mimetype: str (declared; magic-byte validated if python-magic available)
      - size: int (declared; actual size validated from data)
      - data: Optional[bytes] — None means download failed (channel adapter path)
      - id: str (used for fallback filename generation)

    Returns (descriptions, upload_dir, all_writes_failed, image_data):
      - descriptions: list of context strings injected into the prompt
      - upload_dir: container path to clean up after execution, or None
      - all_writes_failed: True iff at least one write was attempted but all failed
      - image_data: list of {"media_type": str, "data": base64_str} for vision blocks
    """
    safe_session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    upload_dir = f"{UPLOAD_BASE}/{safe_session_id}"
    descriptions: List[str] = []
    image_data: List[dict] = []
    dir_created = False
    total_image_bytes = 0
    used_names: set = set()
    write_attempted = 0
    write_succeeded = 0

    for f in raw_files[:max_files]:
        name = f.get("name", "")
        mimetype = f.get("mimetype", "application/octet-stream")
        data: Optional[bytes] = f.get("data")
        file_id = f.get("id", f"file_{len(used_names)}")

        # Handle download failures (channel adapter path)
        if data is None:
            safe_name = sanitize_filename(name, file_id, used_names)
            used_names.add(safe_name)
            descriptions.append(f"{safe_name} — download failed")
            continue

        is_image = mimetype.startswith("image/")

        # Reject unsupported binary formats
        if any(
            mimetype.startswith(m) if m.endswith("/") else mimetype == m
            for m in UNSUPPORTED_MIMES
        ):
            safe_name = sanitize_filename(name, file_id, used_names)
            used_names.add(safe_name)
            descriptions.append(
                f"{safe_name} — unsupported format ({mimetype}). "
                f"Text, CSV, JSON, ZIP, and image files are supported."
            )
            continue

        safe_name = sanitize_filename(name, file_id, used_names)
        used_names.add(safe_name)

        # Actual size validation (TOCTOU defense — declared size is advisory only)
        actual_size = len(data)
        size_limit = max_image_size if is_image else max_file_size
        if actual_size > size_limit:
            logger.warning(f"[UPLOAD] Rejecting {safe_name}: {actual_size} bytes > {size_limit}")
            descriptions.append(
                f"{safe_name} — rejected (exceeds {format_file_size(size_limit)} limit)"
            )
            continue

        # Magic-byte MIME validation
        actual_mime = mimetype
        if _MAGIC_AVAILABLE:
            try:
                detected_mime = magic.from_buffer(data, mime=True)
                declared_is_image = mimetype.startswith("image/")
                detected_is_image = detected_mime.startswith("image/")

                if detected_mime != mimetype:
                    if declared_is_image and detected_is_image:
                        # JPEG vs PNG mislabel — both images, accept with detected MIME
                        logger.debug(
                            f"[UPLOAD] Image MIME mismatch {safe_name}: "
                            f"declared={mimetype}, detected={detected_mime} (allowing)"
                        )
                        actual_mime = detected_mime
                        is_image = True
                    elif mimetype.startswith("text/") and detected_mime.startswith("text/"):
                        # text/plain vs text/csv — both text, accept
                        actual_mime = detected_mime
                    else:
                        logger.warning(
                            f"[UPLOAD] MIME mismatch for {safe_name}: "
                            f"declared={mimetype}, detected={detected_mime}"
                        )
                        descriptions.append(f"{safe_name} — rejected (file type mismatch)")
                        continue
            except Exception as e:
                logger.warning(f"[UPLOAD] MIME detection failed for {safe_name}: {e}")

        size_str = format_file_size(actual_size)

        if is_image:
            if total_image_bytes + actual_size > max_total_image_size:
                descriptions.append(
                    f"{safe_name} ({size_str}) — skipped (total image size limit reached)"
                )
                continue

            write_attempted += 1
            total_image_bytes += actual_size
            b64 = base64.b64encode(data).decode()
            image_data.append({"media_type": actual_mime, "data": b64})
            descriptions.append(
                f"[File uploaded by {uploader}]: {safe_name} ({size_str}) — image provided for visual analysis"
            )
            write_succeeded += 1
            logger.info(f"[UPLOAD] Queued {safe_name} ({size_str}) as vision block for {agent_name}")

            await platform_audit_service.log(
                event_type=AuditEventType.EXECUTION,
                event_action="file_upload",
                source=source,
                target_type="agent",
                target_id=agent_name,
                details={
                    "filename": safe_name,
                    "size_bytes": actual_size,
                    "mime_type": actual_mime,
                    "storage": "stream_json_vision",
                    "sender_id": sender_id,
                    "channel_id": channel_id,
                    "uploader": uploader,
                },
            )

        else:
            # Non-image: write to per-session upload directory in the container
            if not dir_created:
                write_attempted += 1
                try:
                    await container_exec_run(
                        container, f"mkdir -p {upload_dir}", user="developer"
                    )
                    dir_created = True
                except Exception as e:
                    logger.error(
                        f"[UPLOAD] Failed to create {upload_dir} in {agent_name}: {e}"
                    )
                    descriptions.append(
                        f"[File upload failed]: {safe_name} — could not create workspace upload directory"
                    )
                    continue
            else:
                write_attempted += 1

            try:
                tar_buf = io.BytesIO()
                with tarfile.open(fileobj=tar_buf, mode="w") as tar:
                    info = tarfile.TarInfo(name=safe_name)
                    info.size = actual_size
                    info.uid = 1000  # developer user
                    info.gid = 1000
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
                tar_buf.seek(0)

                success = await container_put_archive(container, upload_dir, tar_buf.read())
                if not success:
                    logger.error(f"[UPLOAD] Failed to copy {safe_name} into {agent_name}")
                    descriptions.append(
                        f"[File upload failed]: {safe_name} — could not save to agent workspace"
                    )
                    continue

                dest_path = f"{upload_dir}/{safe_name}"
                descriptions.append(
                    f"[File uploaded by {uploader}]: {safe_name} ({size_str}) saved to {dest_path}"
                )
                write_succeeded += 1
                logger.info(f"[UPLOAD] Copied {safe_name} ({size_str}) to {agent_name}:{dest_path}")

                await platform_audit_service.log(
                    event_type=AuditEventType.EXECUTION,
                    event_action="file_upload",
                    source=source,
                    target_type="agent",
                    target_id=agent_name,
                    details={
                        "filename": safe_name,
                        "size_bytes": actual_size,
                        "mime_type": actual_mime,
                        "storage": "container_file",
                        "dest_path": dest_path,
                        "sender_id": sender_id,
                        "channel_id": channel_id,
                        "uploader": uploader,
                    },
                )

            except Exception as e:
                logger.error(f"[UPLOAD] Error copying {safe_name} to {agent_name}: {e}")
                descriptions.append(
                    f"[File upload failed]: {safe_name} — workspace write error"
                )

    if len(raw_files) > max_files:
        descriptions.append(
            f"({len(raw_files) - max_files} more file(s) skipped — max {max_files} per message)"
        )

    all_writes_failed = write_attempted > 0 and write_succeeded == 0
    return descriptions, upload_dir if dir_created else None, all_writes_failed, image_data


def decode_web_file(f: dict) -> Optional[bytes]:
    """
    Decode a WebFileUpload's base64 data to bytes.

    Handles both raw base64 and data: URI format (data:mime;base64,PAYLOAD)
    emitted by browser FileReader.readAsDataURL().

    Returns None on decode failure.
    """
    raw = f.get("data_base64", "")
    if not raw:
        return None
    try:
        # Strip data: URI prefix if present
        if raw.startswith("data:"):
            comma = raw.index(",")
            raw = raw[comma + 1:]
        return base64.b64decode(raw)
    except Exception as e:
        logger.warning(f"[UPLOAD] base64 decode failed for {f.get('name', '?')}: {e}")
        return None
