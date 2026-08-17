"""Every named volume a non-root service writes needs an ownership story (#2205).

Docker creates named volumes **root-owned**. Trinity's backend and scheduler run as
UID 1000 (Invariant #17 / #874), so a named volume mounted into them is unwritable
unless something chowns it. `#1478` learned this on `trinity-logs` and added a
one-shot init container. `trinity-archives` never got the equivalent, and the
consequence was invisible for two months:

  * `archive_storage.__init__` calls `mkdir(parents=True, exist_ok=True)`, which
    silently no-ops on the existing root-owned directory — so the class logged
    "initialized" and every WRITE failed with `[Errno 13] Permission denied`
  * archived originals were therefore never unlinked, and `/data/logs` grew without
    bound — measured on a real instance at 8.5 GB with a single 4.6 GB file
  * the ERROR went into the very log files archival exists to prune: symptom and
    diagnosis in the same unbounded file nobody tails

This is the #1871-style parity guard the issue asks for, and it is a text scan over
the compose files — no Docker, no imports. It answers one question: does every named
volume mounted into a UID-1000 service have either an init one-shot that chowns it,
or an explicit exemption with a stated reason?

Prod is deliberately DIFFERENT rather than missing: it has no `trinity-archives`
volume at all — `/data/archives` lives inside the `${TRINITY_DATA_PATH}` bind mount
that `scripts/deploy/start.sh` chowns recursively before `up`. Adding a named volume
there without a one-shot would reintroduce the dev bug, which is exactly what this
guard is here to catch.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml"]

# Services that run as UID 1000 and therefore cannot write a root-owned volume.
# (Invariant #17: backend/scheduler are `trinity`, mcp-server is `node`, frontend
# is `nginx` — the first two are the ones with data volumes.)
_NON_ROOT_SERVICES = ("backend", "scheduler")

# Volumes whose ownership is somebody else's problem, with the reason. Anything not
# listed and not covered by an init one-shot fails this test.
_EXEMPT = {
    # Redis runs as its own image user and owns this volume itself; the backend
    # never touches it.
    "redis-data": "mounted only into the redis service, which owns it",
    # Written by the agent containers (UID 1000 by image), not by backend/scheduler.
    "agent-configs": "agent-side volume; backend does not write it",
}


def _strip_comments(text: str) -> str:
    """Drop `#` comment lines. A rule's own documentation must not trip the rule."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _compose_text(name: str) -> str:
    return (_REPO / name).read_text(encoding="utf-8")


def _named_volume_mounts(text: str) -> dict[str, dict[str, str]]:
    """service -> {named volume: container path}. A named volume is a mount whose
    source has no `/` and no `${` (those are bind mounts / interpolated host paths).

    The DESTINATION matters, not just the name — see `_image_prepared_paths`."""
    out: dict[str, dict[str, str]] = {}
    service = None
    in_volumes = False
    for raw in text.splitlines():
        if re.match(r"^  [a-zA-Z0-9_.-]+:\s*$", raw):
            service = raw.strip().rstrip(":")
            in_volumes = False
            continue
        if re.match(r"^    volumes:\s*$", raw):
            in_volumes = True
            continue
        if in_volumes and re.match(r"^    [a-zA-Z]", raw):
            in_volumes = False
        m = re.match(r"^      - ([^:#\s]+):([^:#\s]+)", raw)
        if in_volumes and m and service:
            src = m.group(1)
            if "/" not in src and "${" not in src:
                out.setdefault(service, {})[src] = m.group(2)
    return out


def _service_block(text: str, service: str) -> str:
    """The YAML lines belonging to one service, i.e. up to the next 2-space key.

    Split on the next `\n  <name>:` header rather than on `"\n  "` — the latter
    matches the service's own first indented line and returns an empty block, which
    made an earlier version of this guard report every covered volume as uncovered.
    """
    marker = f"\n  {service}:\n"
    if marker not in text:
        return ""
    rest = text.split(marker, 1)[1]
    nxt = re.search(r"^  [a-zA-Z0-9_.-]+:\s*$", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _init_chowned_volumes(text: str) -> set[str]:
    """Volumes chowned by an `*-init` one-shot in this compose file."""
    chowned: set[str] = set()
    for service, vols in _named_volume_mounts(text).items():
        if not service.endswith("-init"):
            continue
        # Mounting is not covering: the one-shot must actually chown to 1000.
        if "chown 1000:1000" in _service_block(text, service) or \
                "chown -R 1000:1000" in _service_block(text, service):
            chowned |= set(vols)
    return chowned



def _image_prepared_paths() -> set[str]:
    """Container paths the backend image creates AND chowns before `USER trinity`.

    This is the mechanism that makes `trinity-data` fine and `trinity-archives`
    broken, and it is subtler than "named volumes are root-owned": Docker seeds an
    EMPTY named volume from the image's directory at that path, ownership included.
    `docker/backend/Dockerfile` does `mkdir -p /data /agent-configs && chown -R
    trinity:trinity ...`, so a volume mounted AT `/data` inherits 1000:1000 — while
    one mounted at the NESTED `/data/archives`, which the image never creates, has
    nothing to inherit from and stays root-owned. Verified live: `/data` is
    `trinity trinity`, `/data/archives` was `root root`.
    """
    text = (_REPO / "docker" / "backend" / "Dockerfile").read_text(encoding="utf-8")
    if "chown -R trinity:trinity" not in text:
        return set()          # ownership story changed shape — fail closed, loudly
    prepared: set[str] = set()
    for m in re.finditer(r"mkdir -p ([^&\n]+)", text):
        for token in m.group(1).split():
            if token.startswith("/"):
                prepared.add(token.rstrip("/"))
    return prepared


@pytest.mark.parametrize("compose", _COMPOSE_FILES)
def test_every_non_root_volume_has_an_ownership_story(compose):
    text = _compose_text(compose)
    mounts = _named_volume_mounts(text)
    chowned = _init_chowned_volumes(text)

    prepared = _image_prepared_paths()
    missing: list[str] = []
    for service in _NON_ROOT_SERVICES:
        for vol, dest in sorted(mounts.get(service, {}).items()):
            if vol in chowned or vol in _EXEMPT or dest.rstrip("/") in prepared:
                continue
            missing.append(
                f"{compose}: '{vol}' -> '{dest}' in '{service}' (UID 1000)"
            )

    assert not missing, (
        "A named Docker volume is created ROOT-owned, so a UID-1000 service cannot "
        "write it — silently, because `mkdir(exist_ok=True)` succeeds on someone "
        "else's directory and only the first write fails (#2205, #1478).\n"
        "Add an `*-init` one-shot that chowns it to 1000:1000 (see `logs-init` / "
        "`archives-init`), or add it to `_EXEMPT` above WITH the reason.\n\n"
        + "\n".join(missing)
    )


def test_the_archives_volume_is_covered_in_dev():
    """The specific regression: `trinity-archives` mounted into the backend with no
    one-shot is what shipped, so pin the fix rather than only the general rule."""
    text = _compose_text("docker-compose.yml")
    assert "trinity-archives" in _named_volume_mounts(text).get("backend", {})
    assert "trinity-archives" in _init_chowned_volumes(text), (
        "archives-init must chown trinity-archives — without it log archival fails "
        "on every run and /data/logs grows unbounded"
    )


def test_the_backend_waits_for_both_init_one_shots():
    """A chown that races the first archival run is not a fix. Both one-shots are
    `service_completed_successfully` dependencies of the backend."""
    text = _compose_text("docker-compose.yml")
    backend = text.split("\n  backend:", 1)[1].split("\n  vector:", 1)[0]
    for one_shot in ("logs-init", "archives-init"):
        assert one_shot in backend, one_shot
    assert backend.count("service_completed_successfully") >= 2


def test_prod_keeps_archives_on_the_chowned_bind_mount():
    """Prod's story is different, not absent — and the difference is the thing that
    would silently rot if someone 'harmonised' the two files.

    Scanned with comments STRIPPED: prod's mount carries a comment naming
    `trinity-archives` to explain why it is absent, and a raw substring check
    matched that explanation — a guard that fails on the documentation of the rule
    it enforces is worse than none.
    """
    code = _strip_comments(_compose_text("docker-compose.prod.yml"))
    assert "trinity-archives" not in code, (
        "prod grew a trinity-archives volume — it needs an archives-init one-shot "
        "too, or /data/archives stops being inside the chowned bind mount (#2205)"
    )
    assert "/data" in code


def test_the_guard_would_have_caught_the_original_bug():
    """A guard never shown to fail is decoration. Reconstruct the pre-fix compose —
    archives mounted, no one-shot — and assert the rule rejects it."""
    text = _compose_text("docker-compose.yml")
    block = _service_block(text, "archives-init")
    assert block, "the one-shot must exist for this reconstruction to mean anything"
    pre_fix = text.replace(f"\n  archives-init:\n{block}", "\n", 1)

    assert "trinity-archives" in _named_volume_mounts(pre_fix).get("backend", {})
    assert "trinity-archives" not in _init_chowned_volumes(pre_fix), (
        "the pre-fix reconstruction still looks covered — the excision failed"
    )
    # And the nested mount is NOT rescued by the image-prepared `/data`, which is
    # the whole subtlety: `/data` is prepared, `/data/archives` is not.
    assert "/data/archives" not in _image_prepared_paths()
