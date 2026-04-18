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
import tarfile
from fnmatch import fnmatch
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

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
    home_resolved = home_dir.resolve()
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
            target = (home_dir / name).resolve()
            try:
                target.relative_to(home_resolved)
            except ValueError:
                skipped.append(name)
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                skipped.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
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

    tar_bytes, files = build_snapshot(_HOME, patterns)

    snapshot_path_str = ""
    backup_rel = body.get("backup_dir")
    if backup_rel:
        backup_dir = (_HOME / backup_rel).resolve()
        try:
            backup_dir.relative_to(_HOME.resolve())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="backup_dir must be inside /home/developer",
            ) from exc
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "snapshot.tar").write_bytes(tar_bytes)
        (backup_dir / "files.txt").write_text("\n".join(files) + "\n")
        snapshot_path_str = str(
            (backup_dir / "snapshot.tar").relative_to(_HOME)
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
