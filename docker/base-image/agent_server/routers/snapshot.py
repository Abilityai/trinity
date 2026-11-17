"""Snapshot / restore primitives (S3, #384).

These are the two primitives that the reset-preserve-state routine
(#384 / S3) composes with the persistent-state allowlist added in #383:

- `build_snapshot(home_dir, paths)` captures every file under `home_dir`
  whose POSIX-relative path matches any `paths` glob.
- `restore_from_tar(home_dir, tar_bytes, paths)` extracts a tar back into
  `home_dir`, enforcing the same allowlist and rejecting path-traversal
  entries.

Both helpers are pure so they can be mirrored in host-side unit tests; the
HTTP surface is a thin wrapper. The allowlist reader from #383 lives in
`files.py` and is consumed by the composing routine, not by these
primitives.
"""
import io
import json
import logging
import os
import tarfile
from fnmatch import fnmatch
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response


def _write_bytes_nofollow(rel: str, data: bytes, *, base: Path) -> None:
    """`files.write_bytes_nofollow` (ent#822). Imported through the package
    when there is one; by file path otherwise, so these primitives stay pure
    — loadable with no package context, as `files.py` itself is (#1795)."""
    if __package__:
        from .files import write_bytes_nofollow
    else:
        import importlib.util
        import sys
        name = "trinity_agent_files_router"
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, str(Path(__file__).resolve().parent / "files.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[name] = module
        write_bytes_nofollow = sys.modules[name].write_bytes_nofollow
    write_bytes_nofollow(rel, data, base=base)

logger = logging.getLogger(__name__)
router = APIRouter()

_HOME = Path("/home/developer")


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    """True if rel_path matches any glob. Traversal/absolute patterns skipped."""
    for pattern in patterns:
        if pattern.startswith("/") or ".." in pattern.split("/"):
            continue
        prefix = pattern.rstrip("/*")
        if pattern.endswith("/**") and (
            rel_path == prefix or rel_path.startswith(prefix + "/")
        ):
            return True
        if fnmatch(rel_path, pattern):
            return True
    return False


def _collect_files(home_dir: Path, patterns: list[str]) -> list[str]:
    """Walk home_dir, return sorted relative POSIX paths matching any pattern."""
    collected: list[str] = []
    if not patterns:
        return collected
    for candidate in home_dir.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            rel = candidate.relative_to(home_dir).as_posix()
        except ValueError:
            continue
        if ".." in rel.split("/"):
            continue
        if _matches_any(rel, patterns):
            collected.append(rel)
    return sorted(set(collected))


def build_snapshot(home_dir: Path, paths: list[str]) -> tuple[bytes, list[str]]:
    """Pure function — capture matching files into a tar and return the bytes."""
    files = _collect_files(home_dir, paths)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel in files:
            tf.add(home_dir / rel, arcname=rel)
    return buf.getvalue(), files


def restore_from_tar(
    home_dir: Path, tar_bytes: bytes, paths: list[str]
) -> tuple[list[str], list[str]]:
    """Extract tar entries that match paths into home_dir. Returns (restored, skipped)."""
    restored: list[str] = []
    skipped: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tf:
        for member in tf.getmembers():
            name = member.name
            if (
                name.startswith("/")
                or ".." in Path(name).parts
                or not _matches_any(name, paths)
            ):
                skipped.append(name)
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                skipped.append(name)
                continue
            # ent#822: written without following a link, so a link inside the
            # home (a parent directory, or the file itself) cannot send the
            # member onto another file there. That member is skipped; the rest
            # still restore.
            try:
                _write_bytes_nofollow(name, extracted.read(), base=home_dir)
            except HTTPException:
                skipped.append(name)
                continue
            restored.append(name)
    return sorted(restored), sorted(skipped)


@router.post("/api/agent-server/snapshot")
async def snapshot_endpoint(body: dict):
    """Capture paths matching the allowlist to a tarball.

    Body:
      {
        "paths": ["workspace/**", ".trinity/**", ...],
        "backup_dir": "relative/to/home" (optional — also writes copy to disk)
      }
    """
    patterns = body.get("paths") or []
    if not isinstance(patterns, list):
        raise HTTPException(
            status_code=400, detail="paths must be a list of glob patterns"
        )

    backup_rel = body.get("backup_dir")
    if backup_rel is not None and (
        not isinstance(backup_rel, str) or "\x00" in backup_rel
    ):
        raise HTTPException(
            status_code=400, detail="backup_dir must be a path string"
        )

    tar_bytes, files = build_snapshot(_HOME, patterns)

    snapshot_path_str = ""
    if backup_rel:
        # A lexical barrier first (CodeQL py/path-injection): both sides are
        # normalised in the same unresolved space and end in a separator, so
        # `../home2` cannot pass as a prefix of `home/` and `.` (the home itself)
        # still does. The `resolve()` + `relative_to` check below stays: it is
        # the one that sees a link inside the home pointing out of it.
        base = os.path.join(os.path.normpath(str(_HOME)), "")
        full = os.path.join(os.path.normpath(os.path.join(base, backup_rel)), "")
        if not full.startswith(base):
            raise HTTPException(
                status_code=400,
                detail="backup_dir must be inside /home/developer",
            )
        backup_dir = Path(full).resolve()
        try:
            backup_dir.relative_to(_HOME.resolve())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="backup_dir must be inside /home/developer",
            ) from exc
        # Both files go in through ent#822's writer: the directories are walked
        # (and created) one component at a time without following a link, and
        # a link at any level, either file included, is a 403 rather than a
        # write that lands somewhere else.
        rel = os.path.relpath(full, base)
        _write_bytes_nofollow(os.path.join(rel, "snapshot.tar"), tar_bytes, base=_HOME)
        _write_bytes_nofollow(
            os.path.join(rel, "files.txt"),
            ("\n".join(files) + "\n").encode("utf-8"),
            base=_HOME,
        )
        snapshot_path_str = str(
            (backup_dir / "snapshot.tar").relative_to(_HOME.resolve())
        )

    return Response(
        content=tar_bytes,
        media_type="application/x-tar",
        headers={
            "X-Files-Count": str(len(files)),
            "X-Snapshot-Path": snapshot_path_str,
        },
    )


@router.post("/api/agent-server/restore")
async def restore_endpoint(
    tarball: UploadFile = File(...),
    paths: str = Form(...),
):
    """Restore a tarball into /home/developer, enforcing the paths allowlist.

    `paths` is a JSON-encoded list of glob patterns; tar entries that don't
    match any pattern — or that try to escape /home/developer via absolute
    paths or `..` segments — are reported as skipped.
    """
    try:
        patterns = json.loads(paths)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"paths must be a JSON array: {exc}"
        ) from exc
    if not isinstance(patterns, list):
        raise HTTPException(
            status_code=400, detail="paths must be a JSON array"
        )

    blob = await tarball.read()
    restored, skipped = restore_from_tar(_HOME, blob, patterns)
    return {"restored": restored, "skipped_outside_allowlist": skipped}
