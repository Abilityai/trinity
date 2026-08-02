"""Sanitized, read-only delivery-conductor pipeline-state projection."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, Iterator

from .identifiers import is_safe_identifier


_PIPELINE_ID = "delivery-conductor"
_INSTANCE_ID = "current"
_MAX_PROJECTION_BYTES = 256 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ProjectionError(RuntimeError):
    """Raised without exposing malformed durable data or partial projection state."""


def publish_current_projection(
    workspace: str | Path,
    database_path: str | Path,
    now: datetime,
) -> Path:
    """Atomically replace the fixed current projection from one SQLite snapshot."""
    target = _projection_path(Path(workspace))
    with _publication_lock(target):
        return _publish_locked(target, Path(database_path), now)


def _publish_locked(target: Path, database_path: Path, now: datetime) -> Path:
    try:
        projection = _read_projection(database_path, now)
        message = json.dumps(
            projection,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except ProjectionError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise ProjectionError("durable projection state is invalid") from error
    if len(message.encode("utf-8")) > _MAX_PROJECTION_BYTES:
        raise ProjectionError("sanitized projection exceeds 256 KiB")

    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=".current.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(message)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProjectionError("atomic projection replace failed") from error
    return target


@contextmanager
def _publication_lock(target: Path) -> Iterator[None]:
    lock_path = target.parent / ".current.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        path_metadata = lock_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise ProjectionError("projection publication lock is invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked_metadata = lock_path.stat(follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (
            locked_metadata.st_dev,
            locked_metadata.st_ino,
        ):
            raise ProjectionError("projection publication lock is invalid")
        yield
    except ProjectionError:
        raise
    except OSError as error:
        raise ProjectionError("projection publication lock is unavailable") from error
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)


def _read_projection(database_path: Path, now: datetime) -> dict[str, Any]:
    updated_at = _utc_seconds(now)
    try:
        database_uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(
            database_uri, uri=True, isolation_level=None
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            controller = connection.execute(
                """
                SELECT last_fence_token, state, reason_code
                FROM controller_state WHERE singleton = 1
                """
            ).fetchone()
            lease = connection.execute(
                """
                SELECT wake_id, fence_token, claimed_at, expires_at
                FROM repo_lease WHERE singleton = 1
                """
            ).fetchone()
            checkpoint = connection.execute(
                """
                SELECT revision, checkpoint_sha256, acknowledged_wake_id,
                       reason_code, fence_token, run_units_remaining,
                       issue_units_remaining, daily_units_remaining
                FROM run_checkpoint WHERE singleton = 1
                """
            ).fetchone()
            authoritative_wake_id = (
                lease[0]
                if lease is not None
                else (checkpoint[2] if checkpoint else None)
            )
            snapshot = None
            if authoritative_wake_id is not None:
                snapshot = _optional_row(
                    connection,
                    """
                    SELECT snapshot.run_id, snapshot.issue_id,
                           snapshot.signature, snapshot.observed_at_utc,
                           snapshot.attempts, snapshot.repair_cycles,
                           snapshot.run_seconds, snapshot.issue_units,
                           snapshot.daily_units, snapshot.stale_leases,
                           snapshot.orphaned_workers, snapshot.safety_events,
                           snapshot.no_work_ticks,
                           snapshot.deterministic_failures,
                           snapshot.transient_failures,
                           snapshot.run_units_remaining,
                           snapshot.issue_units_remaining,
                           snapshot.daily_units_remaining,
                           snapshot.breaker_state,
                           snapshot.breaker_reason_code,
                           snapshot.breaker_transition_sequence
                    FROM conductor_wake_scope AS binding
                    JOIN conductor_safety_snapshot AS snapshot
                      ON snapshot.run_id = binding.run_id
                     AND snapshot.issue_id = binding.issue_id
                     AND snapshot.signature = binding.signature
                    WHERE binding.wake_id = ?
                    """,
                    (authoritative_wake_id,),
                )
                if snapshot is None:
                    raise ProjectionError(
                        "durable projection state has no correlated safety scope"
                    )
            breaker = None
            if snapshot is not None:
                breaker = _optional_row(
                    connection,
                    """
                    SELECT state, reason_code, transition_sequence, updated_at_utc
                    FROM conductor_breaker_current
                    WHERE run_id = ? AND issue_id = ? AND signature = ?
                    """,
                    (snapshot[0], snapshot[1], snapshot[2]),
                )
            connection.commit()
    except sqlite3.Error as error:
        raise ProjectionError("durable projection state is invalid") from error
    if controller is None:
        raise ProjectionError("durable projection state is invalid")

    controller_view = _controller_view(controller)
    lease_view = _lease_view(lease)
    checkpoint_view = _checkpoint_view(checkpoint)
    safety_view = _safety_view(snapshot, breaker)
    breaker_open = safety_view is not None and safety_view["breaker"]["state"] == "open"
    current_stage = (
        "blocked" if breaker_open else ("leased" if lease_view is not None else "idle")
    )
    health = (
        "blocked" if breaker_open else ("yellow" if lease_view is not None else "green")
    )
    blockers = []
    if breaker_open and safety_view is not None:
        blockers.append(
            {
                "reason_code": safety_view["breaker"]["reason_code"],
                "state": "open",
            }
        )
    return {
        "schema_version": 1,
        "instance_id": _INSTANCE_ID,
        "pipeline_id": _PIPELINE_ID,
        "current_stage": current_stage,
        "health": health,
        "updated_at": updated_at,
        "attempt": 0 if safety_view is None else safety_view["usage"]["attempts"],
        "blockers": blockers,
        "escalations": [],
        "controller": controller_view,
        "lease": lease_view,
        "checkpoint": checkpoint_view,
        "safety": safety_view,
    }


def _controller_view(row: tuple[Any, ...]) -> dict[str, Any]:
    fence_token, state, reason_code = row
    _non_negative_int(fence_token)
    if state not in ("idle", "leased"):
        raise ProjectionError("durable projection state is invalid")
    if reason_code is None:
        reason_code = "lease-active" if state == "leased" else "ready"
    _identifier(reason_code)
    return {
        "state": state,
        "reason_code": reason_code,
        "fence_token": fence_token,
    }


def _lease_view(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    wake_id, fence_token, claimed_at, expires_at = row
    _identifier(wake_id)
    _positive_int(fence_token)
    _stored_utc(claimed_at)
    _stored_utc(expires_at)
    return {
        "wake_id": wake_id,
        "fence_token": fence_token,
        "claimed_at_utc": claimed_at,
        "expires_at_utc": expires_at,
    }


def _checkpoint_view(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    (
        revision,
        digest,
        wake_id,
        reason_code,
        fence_token,
        run_remaining,
        issue_remaining,
        daily_remaining,
    ) = row
    for value in (revision, wake_id, reason_code):
        _identifier(value)
    _digest(digest)
    _positive_int(fence_token)
    for value in (run_remaining, issue_remaining, daily_remaining):
        _non_negative_int(value)
    return {
        "revision": revision,
        "checkpoint_sha256": digest,
        "acknowledged_wake_id": wake_id,
        "reason_code": reason_code,
        "fence_token": fence_token,
        "budget": {
            "run_units_remaining": run_remaining,
            "issue_units_remaining": issue_remaining,
            "daily_units_remaining": daily_remaining,
        },
    }


def _safety_view(
    snapshot: tuple[Any, ...] | None,
    breaker: tuple[Any, ...] | None,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    if breaker is None:
        raise ProjectionError("durable projection state is invalid")
    for value in snapshot[:3]:
        _identifier(value)
    _stored_utc(snapshot[3])
    for value in snapshot[4:18]:
        _non_negative_int(value)
    state, reason_code, sequence, updated_at = breaker
    if state not in ("closed", "open"):
        raise ProjectionError("durable projection state is invalid")
    _identifier(reason_code)
    _non_negative_int(sequence)
    _stored_utc(updated_at)
    usage_names = (
        "attempts",
        "repair_cycles",
        "run_seconds",
        "issue_units",
        "daily_units",
        "stale_leases",
        "orphaned_workers",
        "safety_events",
        "no_work_ticks",
        "deterministic_failures",
        "transient_failures",
    )
    usage = dict(zip(usage_names, snapshot[4:15], strict=True))
    return {
        "scope": {
            "run_id": snapshot[0],
            "issue_id": snapshot[1],
            "signature": snapshot[2],
        },
        "observed_at_utc": snapshot[3],
        "usage": usage,
        "budget": {
            "run_units_remaining": snapshot[15],
            "issue_units_remaining": snapshot[16],
            "daily_units_remaining": snapshot[17],
        },
        "breaker": {
            "state": state,
            "reason_code": reason_code,
            "transition_sequence": sequence,
            "updated_at_utc": updated_at,
        },
    }


def _projection_path(workspace: Path) -> Path:
    if not workspace.exists() or not workspace.is_dir() or workspace.is_symlink():
        raise ProjectionError("projection workspace is invalid")
    root = workspace.resolve()
    target_directory = root
    for part in (".trinity", "pipeline-state", _PIPELINE_ID):
        target_directory = target_directory / part
        if target_directory.exists() and target_directory.is_symlink():
            raise ProjectionError("projection workspace is invalid")
        target_directory.mkdir(mode=0o700, exist_ok=True)
    if not target_directory.resolve().is_relative_to(root):
        raise ProjectionError("projection workspace is invalid")
    if (
        not is_safe_identifier(_PIPELINE_ID)
        or not is_safe_identifier(_INSTANCE_ID)
    ):
        raise ProjectionError("projection identity is invalid")
    return target_directory / f"{_INSTANCE_ID}.json"


def _optional_row(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> tuple[Any, ...] | None:
    try:
        return connection.execute(query, parameters).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise


def _identifier(value: object) -> None:
    if not is_safe_identifier(value):
        raise ProjectionError("durable projection state is invalid")


def _digest(value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProjectionError("durable projection state is invalid")


def _non_negative_int(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ProjectionError("durable projection state is invalid")


def _positive_int(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ProjectionError("durable projection state is invalid")


def _stored_utc(value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProjectionError("durable projection state is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ProjectionError("durable projection state is invalid") from error
    if parsed.utcoffset() != timedelta(0):
        raise ProjectionError("durable projection state is invalid")


def _utc_seconds(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ProjectionError("projection time must be aware UTC")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
