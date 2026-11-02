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
import uuid
from typing import List, Optional, Tuple

from config import ALLOW_PDF_UPLOADS
from services.docker_utils import container_put_archive, container_exec_run
from services.platform_audit_service import platform_audit_service, AuditEventType
from utils.zip_signature import is_zip_container

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
    "application/x-tar",
    "application/gzip", "application/x-rar-compressed",
    "video/", "audio/",
}

UPLOAD_BASE = "/home/developer/uploads"

# A current workspace may heartbeat-sync before its next backend Push installs
# the canonical gitignore defaults. Protect new originals BEFORE writing them.
# info/exclude is local, handles linked worktrees via git's own path resolution,
# and remains lower priority than deliberate user .gitignore overrides.
_PREPARE_UPLOAD_SCRIPT = """
import pathlib, subprocess, sys
home, directory = map(pathlib.Path, sys.argv[1:])
repo = subprocess.run(["git", "-C", str(home), "rev-parse", "--show-toplevel"],
                      capture_output=True, text=True)
if repo.returncode == 0:
    root = pathlib.Path(repo.stdout.strip())
    relative = directory.parent.relative_to(root)
    exclude = subprocess.run(["git", "-C", str(home), "rev-parse", "--git-path", "info/exclude"],
                             capture_output=True, text=True, check=True).stdout.strip()
    target = pathlib.Path(exclude)
    if not target.is_absolute():
        target = home / target
    rule = "/" + relative.as_posix() + "/"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+") as stream:
        stream.seek(0)
        if rule not in stream.read().splitlines():
            stream.write("\\n" + rule + "\\n")
elif (home / ".git").exists():
    raise RuntimeError("could not inspect existing workspace Git metadata")
directory.mkdir(parents=True, exist_ok=False)
"""

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
      - upload_dir: this dispatch's container directory, or None; callers own retention
      - all_writes_failed: True iff delivery was attempted but neither files nor vision succeeded
      - image_data: list of {"media_type": str, "data": base64_str} for vision blocks
    """
    upload_dir = f"{UPLOAD_BASE}/{uuid.uuid4().hex}"
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
        has_pdf_header = bool(re.match(rb"%PDF-[0-9]\.[0-9](?:\r\n?|\n)", data))
        # PDF readers can tolerate a preamble. The disabled policy checks a
        # bounded prefix even when libmagic is missing or cannot identify it.
        if not ALLOW_PDF_UPLOADS and (
            mimetype == "application/pdf"
            or re.search(rb"%PDF-[0-9]\.[0-9](?:\r\n?|\n)", data[:1024])
        ):
            safe_name = sanitize_filename(name, file_id, used_names)
            used_names.add(safe_name)
            descriptions.append(f"{safe_name} — PDF uploads are disabled by the operator")
            continue

        # Reject unsupported binary formats
        if any(
            mimetype.startswith(m) if m.endswith("/") else mimetype == m
            for m in UNSUPPORTED_MIMES
        ):
            safe_name = sanitize_filename(name, file_id, used_names)
            used_names.add(safe_name)
            descriptions.append(
                f"{safe_name} — unsupported format ({mimetype}). "
                f"Text, CSV, JSON, {'PDF, ' if ALLOW_PDF_UPLOADS else ''}ZIP, and image files are supported."
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
        detected_mime = None
        if mimetype == "application/zip" and is_zip_container(data):
            # libmagic 5.46 (Debian trixie) detects ZIPs as application/octet-stream
            # from a buffer, so trust the container signature for a declared ZIP (#3046).
            pass
        elif _MAGIC_AVAILABLE:
            try:
                detected_mime = magic.from_buffer(data, mime=True)
                declared_is_image = mimetype.startswith("image/")
                detected_is_image = detected_mime.startswith("image/")

                if detected_mime != mimetype:
                    if mimetype == "application/octet-stream" and detected_mime == "application/pdf":
                        # Channels may deliver PDF documents with a generic MIME.
                        # Do not extend this exception to other detected formats.
                        actual_mime = detected_mime
                    elif declared_is_image and detected_is_image:
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

        if detected_mime is None and mimetype in {"application/pdf", "application/octet-stream"}:
            # Without libmagic, require the standard PDF header before trusting
            # a PDF declaration or upgrading a generic MIME. This identifies the
            # container only; parsing/extraction remains the receiving agent's job.
            if has_pdf_header:
                actual_mime = "application/pdf"
            elif mimetype == "application/pdf":
                descriptions.append(f"{safe_name} — rejected (file type mismatch)")
                continue

        if actual_mime == "application/pdf" and not ALLOW_PDF_UPLOADS:
            descriptions.append(f"{safe_name} — PDF uploads are disabled by the operator")
            continue

        size_str = format_file_size(actual_size)

        if is_image:
            if total_image_bytes + actual_size > max_total_image_size:
                descriptions.append(
                    f"{safe_name} ({size_str}) — skipped (total image size limit reached)"
                )
                continue

            total_image_bytes += actual_size
            image_data.append({"media_type": actual_mime, "data": base64.b64encode(data).decode()})

        # Originals use the same limits, dispatch directory and retention as
        # existing non-image uploads. Native vision is independent of disk I/O.
        write_attempted += 1
        original_saved = False
        failure_reason = "could not save to agent workspace"
        try:
            if not dir_created:
                exit_code, _ = await container_exec_run(
                    container, ["python3", "-c", _PREPARE_UPLOAD_SCRIPT,
                                "/home/developer", upload_dir], user="developer"
                )
                if exit_code != 0:
                    raise OSError("could not create workspace upload directory")
                dir_created = True
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as tar:
                info = tarfile.TarInfo(name=safe_name)
                info.size = actual_size
                info.uid = 1000
                info.gid = 1000
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
            original_saved = bool(await container_put_archive(container, upload_dir, tar_buf.getvalue()))
        except Exception as e:
            logger.warning("[UPLOAD] Original write failed for %s in %s: %s", agent_name, upload_dir, e)
            failure_reason = "workspace write error"

        dest_path = f"{upload_dir}/{safe_name}" if original_saved else None
        if original_saved:
            description = f"[File uploaded by {uploader}]: {safe_name} ({size_str}) saved to {dest_path}"
        else:
            description = f"[File upload failed]: {safe_name} — {failure_reason}"
        if is_image:
            if not original_saved:
                description = f"[File uploaded by {uploader}]: {safe_name} ({size_str})"
            description += " — image provided for visual analysis"
            if not original_saved:
                description += "; original unavailable in agent workspace"
        descriptions.append(description)
        # Existing callers reject only when NO usable delivery succeeded.
        # A failed original must not discard Claude/Gemini/Codex native vision.
        if original_saved or is_image:
            write_succeeded += 1
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
                "storage": "stream_json_vision" if is_image else "container_file",
                "original_saved": original_saved,
                "dest_path": dest_path,
                "sender_id": sender_id,
                "channel_id": channel_id,
                "uploader": uploader,
            },
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
