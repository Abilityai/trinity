"""
File browser endpoints.
"""
import hashlib
import logging
import errno
import mimetypes
import os
import secrets
import shutil
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel


class FileUpdateRequest(BaseModel):
    """Request body for file updates."""
    content: str

logger = logging.getLogger(__name__)
router = APIRouter()

# The agent's home: the only tree these routes serve. A module constant so the
# handlers can be driven over a temporary home in tests.
_HOME = Path("/home/developer")


def _load_path_policy():
    """The vendored path policy (trinity-enterprise#820), loaded ONCE from its
    file beside the agent_server package. By file path, not a relative import:
    this router must stay loadable with no package context (#1795)."""
    import importlib.util
    import sys
    name = "trinity_file_path_policy"
    if name not in sys.modules:
        source = Path(__file__).resolve().parent.parent / "file_path_policy.py"
        spec = importlib.util.spec_from_file_location(name, str(source))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


_policy = _load_path_policy()


def _home_str() -> str:
    return str(_HOME.resolve())

# trinity-enterprise#819: download and preview never follow a link. The backend
# maps this code to its own refusal and an audit row; keep the code in sync with
# services/agent_service/files.py.
_LINK_REFUSAL = {
    "code": "resolved_path_mismatch",
    "message": "This path is a link. Links are not opened by the file routes; open the file it points to.",
}
_MAX_READ_BYTES = 100 * 1024 * 1024  # 100MB


def _open_for_read(path: str, base: Optional[Path] = None) -> tuple[int, os.stat_result]:
    """Open a regular file under `base` for reading without following any link.

    The path is walked one component at a time with O_NOFOLLOW, each step
    relative to the directory opened before it, and the caller serves the
    descriptor this returns: the file that was checked is the file that is
    read, and a path swapped for a link after the check cannot be read through.
    A path that is a link, or passes through one, is refused with 403
    `resolved_path_mismatch`. Returns (fd, fstat); the caller owns fd.
    """
    base = _HOME if base is None else base
    raw = path if path.startswith("/") else str(base / path)
    # Leading slashes collapsed first: normpath keeps exactly two.
    lexical = Path("/" + os.path.normpath(raw).lstrip("/"))
    if not lexical.is_relative_to(base):
        raise HTTPException(status_code=403, detail="Access denied: only /home/developer accessible")
    parts = lexical.relative_to(base).parts
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_NONBLOCK if last else os.O_DIRECTORY)
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except OSError as e:
                try:
                    is_link = stat_mod.S_ISLNK(os.lstat(part, dir_fd=fd).st_mode)
                except FileNotFoundError:
                    raise HTTPException(status_code=404, detail=f"File not found: {path}") from e
                if is_link:
                    raise HTTPException(status_code=403, detail=dict(_LINK_REFUSAL)) from e
                if e.errno in (errno.ENOTDIR, errno.ELOOP):
                    # a file used as a directory
                    raise HTTPException(status_code=400, detail=f"Not a file: {path}") from e
                raise HTTPException(status_code=500, detail=f"Failed to read file: {e}") from e
            os.close(fd)
            fd = nxt
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")
        return fd, st
    except BaseException:
        os.close(fd)
        raise


def _iso_z_from_mtime(mtime: float) -> str:
    """Format an mtime (epoch seconds) as canonical ISO-Z UTC.

    Defined locally rather than imported from ``..utils.helpers`` so this router
    stays standalone-importable: tests/unit/test_ent183_skill_packages.py loads
    this file by path (no package context) to verify its protected-path logic,
    and a relative import would break that load (#1795).
    """
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# trinity-enterprise#820: the write verbs, and the list root, never act through
# a link that changes what a path is. The backend decides the tier on the path
# the caller NAMED; a link made by the agent (on request, in chat) could make
# that name reach a different class — `workspace/x/CLAUDE.md` with
# `x -> ../.claude`. The path is resolved once, refused unless the resolved
# path has the same class set as the named one, and then acted on by walking
# the resolved path one component at a time without following any link, so a
# link that appears after the check is refused rather than followed.
_MAX_LIST_NODES = 20_000


def _named_path(path: str) -> str:
    """The caller's path, normalised under the home, lexically."""
    return _policy.normalize(path, _home_str())


def _refuse_outside_home(target: Path) -> None:
    if not target.is_relative_to(_HOME.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: only /home/developer accessible")


def _refuse_class_change(named: str, target: Path, verb: Optional[str] = None) -> None:
    """403 `resolved_path_mismatch` when a link changes the path's class, or
    (with `verb`) any tier's verdict for that verb."""
    if not _policy.same_class(named, str(target), _home_str(), verb=verb):
        raise HTTPException(status_code=403, detail=dict(_LINK_REFUSAL))


def _mutation_target(path: str, verb: str) -> tuple[Path, str]:
    """(target, named) for PUT, mkdir and DELETE: the parent resolved, the
    final component NOT followed (a final link is acted on, or refused, as
    itself). Contained in the home and class-checked."""
    named = _named_path(path)
    if not named:
        raise HTTPException(status_code=400, detail="Invalid path")
    lexical = Path(named)
    target = lexical.parent.resolve() / lexical.name
    _refuse_outside_home(target)
    _refuse_class_change(named, target, verb)
    return target, named


def _walk_dir_nofollow(rel_parts, *, create: bool = False) -> int:
    """Open home/<rel_parts> as a directory, one component at a time, never
    following a link; with `create`, make missing components. Returns the fd
    (the caller closes it). A link → 403 `resolved_path_mismatch`; a file used
    as a directory → 400."""
    fd = os.open(_HOME.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in rel_parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as e:
                    raise HTTPException(status_code=500, detail=f"Failed to create folder: {part}: {e.strerror}") from e
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError as e:
                try:
                    mode = os.lstat(part, dir_fd=fd).st_mode
                except FileNotFoundError:
                    raise HTTPException(status_code=404, detail=f"Path not found: {part}") from e
                if stat_mod.S_ISLNK(mode):
                    raise HTTPException(status_code=403, detail=dict(_LINK_REFUSAL)) from e
                if e.errno == errno.ENOTDIR or not stat_mod.S_ISDIR(mode):
                    raise HTTPException(status_code=400, detail=f"Not a directory: {part}") from e
                raise HTTPException(status_code=500, detail=f"Failed to open {part}: {e.strerror}") from e
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _parent_parts(target: Path) -> list:
    return list(target.parent.relative_to(_HOME.resolve()).parts)


def _lstat_at(dir_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


@router.get("/api/files")
async def list_files(path: str = "/home/developer", show_hidden: bool = False):
    """
    List files in the workspace directory recursively.
    Only allows access to /home/developer for security.

    Args:
        path: Directory path to list
        show_hidden: If True, include hidden files (starting with .)

    Returns a hierarchical tree structure with folders and files.
    """
    # Security: Only allow workspace access. Resolved-path containment
    # (#3032: a string prefix lets /home/developer2 through), and a link at the
    # root may not change what is listed (ent#820).
    allowed_base = _HOME.resolve()
    named = _named_path(path) or str(allowed_base)
    requested_path = Path(named).resolve()
    _refuse_outside_home(requested_path)
    _refuse_class_change(named, requested_path, _policy.LIST)

    if not requested_path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    budget = {"left": _MAX_LIST_NODES, "truncated": False}

    def build_tree(directory: Path, base_path: Path, include_hidden: bool) -> dict:
        """Build a hierarchical tree structure from a directory. A link is
        listed as a link and never descended (ent#820); the walk stops after
        _MAX_LIST_NODES nodes."""
        items = []
        total_files = 0

        try:
            # Get all items in directory; a link to a directory sorts as a file
            def _is_real_dir(item: Path) -> bool:
                try:
                    return stat_mod.S_ISDIR(item.lstat().st_mode)
                except OSError:
                    return False

            dir_items = sorted(directory.iterdir(), key=lambda x: (not _is_real_dir(x), x.name.lower()))

            for item in dir_items:
                # Skip hidden items unless show_hidden is True
                if item.name.startswith('.') and not include_hidden:
                    continue
                if budget["left"] <= 0:
                    budget["truncated"] = True
                    break
                budget["left"] -= 1

                try:
                    stat = item.lstat()
                    relative_path = item.relative_to(base_path)

                    if stat_mod.S_ISLNK(stat.st_mode):
                        items.append({
                            "name": item.name,
                            "path": str(relative_path),
                            "type": "link",
                            "modified": _iso_z_from_mtime(stat.st_mtime)
                        })
                    elif stat_mod.S_ISDIR(stat.st_mode):
                        # Recursively build tree for subdirectory
                        subtree = build_tree(item, base_path, include_hidden)
                        items.append({
                            "name": item.name,
                            "path": str(relative_path),
                            "type": "directory",
                            "children": subtree["children"],
                            "file_count": subtree["file_count"],
                            "modified": _iso_z_from_mtime(stat.st_mtime)
                        })
                        total_files += subtree["file_count"]
                    else:
                        # It's a file
                        items.append({
                            "name": item.name,
                            "path": str(relative_path),
                            "type": "file",
                            "size": stat.st_size,
                            "modified": _iso_z_from_mtime(stat.st_mtime)
                        })
                        total_files += 1

                except Exception as e:
                    logger.warning(f"Failed to process item {item}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to read directory {directory}: {e}")

        return {"children": items, "file_count": total_files}

    try:
        tree_data = build_tree(requested_path, allowed_base, show_hidden)

        return {
            "base_path": str(allowed_base),
            "requested_path": str(requested_path.relative_to(allowed_base)) if requested_path != allowed_base else ".",
            "tree": tree_data["children"],
            "total_files": tree_data["file_count"],
            "show_hidden": show_hidden,
            "truncated": budget["truncated"],
        }

    except Exception as e:
        logger.error(f"File listing error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


@router.get("/api/files/download")
async def download_file(path: str):
    """
    Download a file from the workspace.
    Only allows access to /home/developer for security.
    Max file size: 100MB

    Returns file content as plain text. Never follows a link (ent#819): the
    file is opened without following any link and that descriptor is read.
    """
    fd, st = _open_for_read(path)
    with os.fdopen(fd, "rb") as f:
        if st.st_size > _MAX_READ_BYTES:
            raise HTTPException(
                status_code=413, detail=f"File too large: {st.st_size} bytes (max {_MAX_READ_BYTES})"
            )
        try:
            # At most the size it had when opened: a file that grows is not read past it.
            content = f.read(st.st_size).decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"File download error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to read file: {str(e)}")
    # ent#820: the backend refuses a file with more than one name below the
    # owner tier (a hard link to a credential); it needs the count to do so.
    return PlainTextResponse(content=content, headers={"X-File-Nlink": str(st.st_nlink)})


# Protected paths that cannot be deleted. By name on the path or any parent —
# the agent server's platform floor, from the vendored policy
# (trinity-enterprise#820; `.credentials.enc` joined it there).
PROTECTED_PATHS = list(_policy.AGENT_SERVER_DELETE_PROTECTED_NAMES)

# Paths that cannot be edited via the file-write endpoint.
#
# .mcp.json and .mcp.json.template were historically editable here because
# "users need to modify them" — but raw editing of either is RCE-by-config:
# tool `command:` fields run as the agent process. See #590 (AISEC-C2).
#
# The sanctioned path (#2008 — this comment previously named
# `/api/credentials/update`, which had no callers anywhere and was removed):
#   * `POST /api/agents/{name}/credentials/inject`, gated by
#     `validate_mcp_config` — Layer 2 of the #590 closure (#598). Real today.
#
# PLANNED, not yet available: rendering `.mcp.json.template` into `.mcp.json`
# at container startup — #2007 / PR #2013.
#
# CLAUDE.md is editable here: who may edit it is a TIER the backend decides
# (owner tier through the Files tab, ent#820); this server cannot tell callers
# apart. It is deletable by no one.
EDIT_PROTECTED_PATHS = list(_policy.AGENT_SERVER_EDIT_PROTECTED_NAMES)
# trinity-enterprise#823: runtime config these routes never write, matched on
# the RESOLVED path (relative to `_HOME`).
_RUNTIME_CONFIG_PATHS = _policy.AGENT_SERVER_RUNTIME_CONFIG_RELPATHS


def _touches_runtime_config(resolved: Path, *, include_ancestors: bool) -> bool:
    """True when `resolved` is a runtime config path or lies under one; with
    `include_ancestors` (DELETE), also when it is a directory above one."""
    home = _HOME.resolve()
    if resolved.is_relative_to(home):
        rel = resolved.relative_to(home).as_posix()
        return _policy.agent_server_touches_runtime_config(
            "" if rel == "." else rel, include_ancestors=include_ancestors)
    return include_ancestors and home.is_relative_to(resolved)


def _is_protected_path(path: Path) -> bool:
    """Check if path is a protected file/directory (for deletion)."""
    return _policy.agent_server_delete_protected(path.as_posix())


def _is_edit_protected_path(path: Path) -> bool:
    """Check if path is protected from editing."""
    return _policy.agent_server_edit_protected(path.as_posix())


# ---------------------------------------------------------------------------
# S4 — Persistent State Allowlist reader (abilityai/trinity#383)
#
# Reads the list of workspace paths that must survive a template-level reset
# from `.trinity/persistent-state.yaml`, falling back to the default list
# when the file is missing, empty, or malformed. The file is materialized at
# agent creation by backend `services.git_service.materialize_persistent_state`.
#
# This PR introduces the reader primitive only — it is INTENTIONALLY not
# wired into `PROTECTED_PATHS` / `EDIT_PROTECTED_PATHS`. Delete/edit
# protection semantics are unchanged. The reset-preserve-state operation
# (#384) is the PR that consumes this reader for protection decisions.
# ---------------------------------------------------------------------------

_PERSISTENT_STATE_PATH = Path("/home/developer/.trinity/persistent-state.yaml")

_DEFAULT_PERSISTENT_STATE = [
    "workspace/**",
    ".trinity/**",
    ".mcp.json",
    ".claude.json",
    ".claude/.credentials.json",
]


def _read_persistent_state() -> list[str]:
    """Read the persistent-state allowlist from disk, with defaults."""
    import yaml

    # #1965 + #1795: imported HERE, not at module scope. `files.py` must stay
    # loadable by `spec_from_file_location` with NO package context — a
    # module-level `from ..safe_yaml import …` raises `attempted relative import
    # with no known parent package` and breaks
    # `test_files_router_stays_standalone_importable` and the ent#183
    # protected-path test. That constraint is why this file duplicates
    # `_iso_z_from_mtime` instead of importing it, and why `yaml` is already
    # function-local above. In production the package context always exists, so
    # the deferred import is free.
    from ..safe_yaml import AliasPolicy, HardenedYamlError, load_hardened_yaml

    if not _PERSISTENT_STATE_PATH.exists():
        return list(_DEFAULT_PERSISTENT_STATE)
    try:
        # #1965: `~/.trinity/persistent-state.yaml` is written by the agent
        # itself (S4, #383) — the most agent-writable document of the set — and
        # the returned patterns drive what survives a reset. REJECT: no
        # legitimate allowlist needs an anchor.
        #
        # HardenedYamlError is a ValueError, NOT a YAMLError, so it is named in
        # the except tuple explicitly. Falling back to the defaults on a refused
        # document is the right failure here: this helper already treats an
        # unreadable file as "use the defaults", and a bomb is a species of
        # unreadable.
        data = load_hardened_yaml(
            _PERSISTENT_STATE_PATH.read_text(),
            kind="persistent_state",
            alias_policy=AliasPolicy.REJECT,
        ) or {}
    except (OSError, yaml.YAMLError, HardenedYamlError):
        return list(_DEFAULT_PERSISTENT_STATE)
    patterns = data.get("persistent_state")
    if not isinstance(patterns, list) or not patterns:
        return list(_DEFAULT_PERSISTENT_STATE)
    return [str(p) for p in patterns]


@router.delete("/api/files")
async def delete_file(path: str):
    """
    Delete a file or directory from the workspace.
    Only allows access to /home/developer for security.
    Cannot delete protected paths (CLAUDE.md, .trinity, .git, etc.)

    A link is deleted as itself, never its target; a link on the way there
    that changes the path's class is refused (ent#820). The directory removal
    is fd-based (`shutil.rmtree(dir_fd=...)`), so it does not follow a link
    swapped in mid-walk.
    """
    target, _named = _mutation_target(path, _policy.DELETE)

    # Prevent deleting the base directory itself
    if target == _HOME.resolve():
        raise HTTPException(status_code=403, detail="Cannot delete home directory")

    # Check if it's a protected path, or a directory holding runtime config
    if _is_protected_path(target) or _touches_runtime_config(target, include_ancestors=True):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot delete protected path: {target.name}"
        )

    parent_fd = _walk_dir_nofollow(_parent_parts(target))
    try:
        st = _lstat_at(parent_fd, target.name)
        if st is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        try:
            if stat_mod.S_ISDIR(st.st_mode):
                file_type = "directory"
                file_count = sum(
                    len(dirs) + len(files)
                    for _root, dirs, files, _fd in os.fwalk(target.name, dir_fd=parent_fd)
                )
                shutil.rmtree(target.name, dir_fd=parent_fd)
            else:
                file_type = "link" if stat_mod.S_ISLNK(st.st_mode) else "file"
                file_count = 1
                os.unlink(target.name, dir_fd=parent_fd)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"File delete error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")
    finally:
        os.close(parent_fd)

    logger.info(f"Deleted {file_type}: {target}")
    return {
        "success": True,
        "deleted": path,
        "type": file_type,
        "file_count": file_count
    }


@router.get("/api/files/preview")
async def preview_file(path: str):
    """
    Get file with proper MIME type for preview.
    Supports images, videos, audio, PDFs, and text files.
    Only allows access to /home/developer for security.
    Max file size: 100MB

    Never follows a link (ent#819): the file is opened without following any
    link and that descriptor is streamed, then closed.
    """
    fd, st = _open_for_read(path)
    f = os.fdopen(fd, "rb")
    if st.st_size > _MAX_READ_BYTES:
        f.close()
        raise HTTPException(
            status_code=413, detail=f"File too large: {st.st_size} bytes (max {_MAX_READ_BYTES})"
        )

    name = Path(os.path.normpath(path)).name
    # Detect MIME type; default to binary for unknown types
    mime_type, _ = mimetypes.guess_type(name)
    if mime_type is None:
        mime_type = "application/octet-stream"

    def _chunks():
        # At most the declared Content-Length: a file that grows is not read past it.
        remaining = st.st_size
        try:
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            f.close()

    # Inline disposition so text/media files render in the preview panel
    # rather than triggering a download.
    return StreamingResponse(
        _chunks(),
        media_type=mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Content-Length": str(st.st_size),
            "X-File-Nlink": str(st.st_nlink),
        },
    )


def content_sha256(text: str) -> str:
    """The hash both sides of the compare-and-swap agree on (#2915): sha256 of
    the UTF-8 text — what the download endpoint hands out as `response.text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text_at(dir_fd: int, name: str) -> Optional[str]:
    """The file's text as the download endpoint serves it, opened without
    following a link; None when it does not exist."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _if_match_holds_at(dir_fd: int, name: str, if_match: Optional[str]) -> bool:
    if if_match is None:
        return True
    try:
        text = _read_text_at(dir_fd, name)
    except OSError:
        return False
    return text is None or content_sha256(text) == if_match


def if_match_holds(path: Path, if_match: Optional[str]) -> bool:
    """True when the caller's `if_match` still describes the file on disk.

    No `if_match` → always holds (a plain write). A file that does not exist
    holds too (there is nothing to clobber). The bytes are decoded exactly as
    the download endpoint serves them (`errors="replace"`), so both sides hash
    the same text and a stray non-UTF-8 byte cannot wedge delivery behind an
    endless 412 (#2989 review). An unreadable file never matches; the file is
    opened without following a link (ent#820).
    """
    if if_match is None:
        return True
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return _if_match_holds_at(dir_fd, path.name, if_match)
    finally:
        os.close(dir_fd)


def _write_text_atomic_at(dir_fd: int, name: str, content: str) -> None:
    """tmp + os.replace inside the opened directory. The tmp name is random and
    created O_EXCL|O_NOFOLLOW, so neither a crash leftover nor a planted link
    blocks or redirects the write; an existing file keeps its mode (ent#820)."""
    old = _lstat_at(dir_fd, name)
    tmp_name = f".{name}.tmp-{secrets.token_hex(6)}"
    fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o644, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8"))
            if old is not None and stat_mod.S_ISREG(old.st_mode):
                os.fchmod(f.fileno(), stat_mod.S_IMODE(old.st_mode))
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def write_text_atomic(path: Path, content: str) -> None:
    """tmp + os.replace in the same directory: a reader never sees a torn file,
    and a concurrent writer's own read is of a whole file (#2915)."""
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _write_text_atomic_at(dir_fd, path.name, content)
    finally:
        os.close(dir_fd)


@router.put("/api/files")
async def update_file(
    path: str,
    request: FileUpdateRequest,
    platform: bool = False,
    if_match: Optional[str] = None,
):
    """
    Update or create a file's content in the workspace.
    Only allows access to /home/developer for security.
    Cannot modify protected paths (.trinity, .git, etc.) unless platform=true.
    Creates parent directories if they don't exist.

    #2915 — the write is ATOMIC (tmp + os.replace) and optionally a
    compare-and-swap: `if_match` is the sha256 hex of the UTF-8 text the caller
    last read; when the file exists and no longer matches, the write is refused
    with 412 and nothing is touched. The platform's operator-queue write-back
    uses this so it can never clobber an entry the agent appended between the
    platform's read and its write — both writers used to be plain overwrites.

    Args:
        path: File path to update/create (query parameter)
        request: Request body with content
        platform: If true, allows writes to .trinity directory (platform-initiated)
        if_match: sha256 hex of the content the caller last read (see above)

    Returns:
        Success status and file info
    """
    # Security: Only allow workspace access, by resolved-path containment
    # (#2915), and never through a link that changes the path's class
    # (ent#820). The final component is not followed: a link there is refused.
    requested_path, _named = _mutation_target(path, _policy.WRITE)

    # The home itself is a directory, not a file to write: a named 400, as
    # mkdir and DELETE answer, not the 500 its missing parent raised (ent#949).
    if requested_path == _HOME.resolve():
        raise HTTPException(status_code=400, detail="Not a file: /home/developer")

    # Check if it's a protected path (for editing)
    # Platform writes can bypass .trinity protection (but not .git, .env, etc.)
    if _is_edit_protected_path(requested_path):
        # Allow platform writes to .trinity directory only
        is_trinity_path = any(p.name == ".trinity" for p in [requested_path] + list(requested_path.parents))
        if not (platform and is_trinity_path):
            raise HTTPException(
                status_code=403,
                detail=f"Cannot edit protected path: {requested_path.name}"
            )

    # ent#823: runtime config is never written here, platform or not.
    if _touches_runtime_config(requested_path, include_ancestors=False):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot edit protected path: {requested_path.name}"
        )

    # Create parent directories if they don't exist, never through a link.
    parent_fd = _walk_dir_nofollow(_parent_parts(requested_path), create=True)
    try:
        existing = _lstat_at(parent_fd, requested_path.name)
        if existing is not None and stat_mod.S_ISLNK(existing.st_mode):
            raise HTTPException(status_code=403, detail=dict(_LINK_REFUSAL))
        if existing is not None and not stat_mod.S_ISREG(existing.st_mode):
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")

        # #2915: compare-and-swap. Hash the same bytes the download endpoint
        # hands out (the UTF-8 text), so the platform's sha over
        # `response.text` and this one agree; a file that cannot be read never
        # matches, which fails closed (412) rather than open.
        if not _if_match_holds_at(parent_fd, requested_path.name, if_match):
            raise HTTPException(
                status_code=412,
                detail="if_match mismatch: the file changed since it was read",
            )

        try:
            # Write the new content ATOMICALLY: a reader (or a concurrent
            # writer's own read) never sees a half-written file.
            _write_text_atomic_at(parent_fd, requested_path.name, request.content)
            stat = os.stat(requested_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except Exception as e:
            logger.error(f"File update error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to update file: {str(e)}")
    finally:
        os.close(parent_fd)

    logger.info(f"Updated file: {requested_path}")
    return {
        "success": True,
        "path": path,
        "size": stat.st_size,
        "modified": _iso_z_from_mtime(stat.st_mtime)
    }


@router.post("/api/files/mkdir")
async def create_folder(path: str):
    """
    Create a new directory in the workspace.
    Only allows access to /home/developer for security.
    Cannot create inside protected paths (.trinity, .git, etc.).
    Creates intermediate parent directories. Rejects if the target
    directory already exists (409).

    Args:
        path: Directory path to create (query parameter)

    Returns:
        Success status and directory info
    """
    # Security: Only allow workspace access. Resolved-path containment via
    # is_relative_to() rather than str.startswith(), which has a sibling-prefix
    # bypass (e.g. "/home/developer-x".startswith("/home/developer") is True).
    # This also acts as the CWE-022 path-traversal barrier. Never through a
    # link that changes the path's class (ent#820). (CodeQL py/path-injection)
    allowed_base = _HOME.resolve()
    requested_path, _named = _mutation_target(path, _policy.WRITE)

    # Cannot create the home directory itself
    if requested_path == allowed_base:
        raise HTTPException(status_code=400, detail="Directory already exists: /home/developer")

    # Reject creation inside an edit-protected path (.trinity, .git, etc.).
    # _is_edit_protected_path walks parents, so a nested target under a
    # protected dir is rejected too.
    if _is_edit_protected_path(requested_path) or _touches_runtime_config(
        requested_path, include_ancestors=False
    ):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot create folder in protected path: {requested_path.name}"
        )

    parent_fd = _walk_dir_nofollow(_parent_parts(requested_path), create=True)
    try:
        existing = _lstat_at(parent_fd, requested_path.name)
        if existing is not None:
            if stat_mod.S_ISLNK(existing.st_mode):
                raise HTTPException(status_code=403, detail=dict(_LINK_REFUSAL))
            if stat_mod.S_ISDIR(existing.st_mode):
                raise HTTPException(status_code=409, detail=f"Directory already exists: {path}")
            raise HTTPException(status_code=409, detail=f"A file already exists at: {path}")
        try:
            os.mkdir(requested_path.name, dir_fd=parent_fd)
            stat = os.stat(requested_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except Exception as e:
            logger.error(f"Folder create error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to create folder: {str(e)}")
    finally:
        os.close(parent_fd)

    logger.info(f"Created directory: {requested_path}")
    return {
        "success": True,
        "path": path,
        "type": "directory",
        "modified": _iso_z_from_mtime(stat.st_mtime)
    }
