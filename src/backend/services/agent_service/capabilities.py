"""
Linux capability sets for agent containers (Issue #602 — Phase 3c).

This module is intentionally stdlib-only and import-light so it can be
exercised by `tests/unit/test_capability_set.py` without dragging the
docker / fastapi / database transitive imports of `lifecycle.py` into
the test runner. `lifecycle.py` re-exports these names for callers.

Trinity always launches agent containers with `cap_drop=ALL` and then
re-adds one of these sets. The pattern (defense in depth):

    cap_drop = ['ALL']
    cap_add  = FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES
"""

from __future__ import annotations

import os
import re


# Restricted mode capabilities - minimum for agent operation (default)
RESTRICTED_CAPABILITIES: list[str] = [
    'NET_BIND_SERVICE',  # Bind to ports < 1024
    'SETGID', 'SETUID',  # Change user/group (for su/sudo)
    'CHOWN',             # Change file ownership
    'SYS_CHROOT',        # Use chroot
    'AUDIT_WRITE',       # Write to audit log
]

# Full capabilities mode - adds package installation support.
# Used when agents need apt-get, pip install, etc.
#
# Issue #602 / Phase 3c (cap tightening): four caps removed from this
# set after AISEC-C2 review. The remaining set is the minimum that keeps
# `sudo apt install` working inside an agent container.
#
# Dropped (no defensible agent use case — documented here so a future
# PR doesn't silently re-add them):
#   SYS_PTRACE  Lets a process read another process's memory. A malicious
#               MCP server could read Claude Code's heap and exfil the
#               OAuth token even if the token isn't in env. This is the
#               direct AISEC-C2 escalation path; removing it closes it
#               without waiting for Layer 3b (bubblewrap sandbox).
#   MKNOD       Creates device nodes under /dev. Agents have no use case
#               for /dev/* manipulation; primarily a container-escape
#               primitive (e.g. creating a writable raw disk device).
#   NET_RAW     Raw / ICMP sockets. Trinity's "ping another agent" UX is
#               HTTP-level, not ICMP. Removing this prevents raw-packet
#               crafting (TCP RST injection, ARP spoofing on the docker
#               bridge, etc.).
#   FSETID      Lets a process keep setuid/setgid bits on chmod after a
#               non-owner write — used to plant a setuid binary the next
#               privileged path can run. No agent workflow needs it.
FULL_CAPABILITIES: list[str] = RESTRICTED_CAPABILITIES + [
    'DAC_OVERRIDE',      # Bypass file permission checks (needed for sudo apt)
    'FOWNER',            # Bypass permission checks on file owner
    'KILL',              # Send signals to processes
]

# These capabilities are NEVER granted - they pose significant security risks.
# Listed for documentation; we achieve this by always using cap_drop=['ALL'].
PROHIBITED_CAPABILITIES: list[str] = [
    'SYS_ADMIN',         # Mount filesystems, configure namespace - too powerful
    'NET_ADMIN',         # Network administration - could escape container
    'SYS_RAWIO',         # Raw I/O access - direct hardware access
    'SYS_MODULE',        # Load kernel modules - kernel compromise
    'SYS_BOOT',          # Reboot system
]


# Agent /tmp mount + scratch-space defaults (#1098, #1231)
# -----------------------------------------------------------------------------
# /tmp is a RAM-backed tmpfs, hardened noexec,nosuid. It is deliberately
# non-exec so a compromised agent can't stage/execute payloads there. The
# catch: heavy scratch (pip/npm install, compiling C extensions, ML wheels
# like torch/transformers) must NOT land on /tmp — it hits "No space left on
# device" at the cap, and "Permission denied" on the noexec flag. #1098
# redirects $TMPDIR-honoring tools off /tmp; but install scripts that hardcode
# /tmp (e.g. the `gh` CLI) still exhaust the cap, silently breaking later /tmp
# writes (incl. git's commit scratch) — #1231.
#
# Size is operator-tunable via AGENT_TMP_SIZE (e.g. "512m", "2g"), default
# 512m. ONLY the size is configurable — noexec,nosuid stay hardcoded (security
# posture), and the value stays bounded (it counts against the container memory
# cgroup). An empty/invalid value falls back to the default rather than
# producing a broken or unbounded mount spec. Mount specs are creation-time, so
# existing agents pick up a new size on recreate, not restart.
#
# Defined here (single source of truth) so the create path (crud.py) and the
# recreate path (lifecycle.py) can't drift — both import this constant.
_AGENT_TMP_SIZE_DEFAULT = "512m"
_AGENT_TMP_SIZE_RE = re.compile(r"^\d+[mg]$")


def _resolve_agent_tmp_size() -> str:
    """Validated /tmp tmpfs size from AGENT_TMP_SIZE (env), else the default.

    Accepts ``<int>m`` / ``<int>g`` (case-insensitive); anything else — empty,
    a bare number, a Kubernetes-style suffix — falls back to the default so a
    typo can never yield a broken or unbounded mount spec.
    """
    raw = (os.getenv("AGENT_TMP_SIZE") or "").strip().lower()
    return raw if _AGENT_TMP_SIZE_RE.match(raw) else _AGENT_TMP_SIZE_DEFAULT


AGENT_TMPFS_MOUNT: dict[str, str] = {
    '/tmp': f'noexec,nosuid,size={_resolve_agent_tmp_size()}'
}

# Default TMPDIR redirects scratch onto the disk-backed, exec-capable agent
# home volume. pip / npm / most build tooling honor TMPDIR, so this dodges both
# the 100 MB cap and noexec in one move while keeping /tmp's hardened posture.
# The directory is created (writable by UID 1000) at container start by
# docker/base-image/startup.sh, so existing agents pick it up on restart.
AGENT_DEFAULT_TMPDIR: str = '/home/developer/.tmp'


# Container resource limits (#1197)
# -----------------------------------------------------------------------------
# Canonical allowed values for the per-agent CPU / memory limits, shared by the
# admin defaults endpoint (routers/settings.py) and the three container-create
# sites (crud.py create, lifecycle.py recreate, system_agent_service.py). Kept
# here — stdlib-only, import-light — so the value set has ONE home and the
# create paths can't drift from what the API accepts.
#
# CPU is an integer processor count fed to Docker's NanoCpus (#1126); memory is
# a Docker memory string fed to mem_limit. A template.yaml carrying a fractional
# or Kubernetes-style value (cpu: "0.5", memory: "512Mi") is rejected up front
# with an actionable message instead of crashing deep in container creation on a
# raw int() / an invalid Docker mem string (#1197).
VALID_CPU: tuple[str, ...] = ("1", "2", "4", "8", "16")
VALID_MEMORY: tuple[str, ...] = ("1g", "2g", "4g", "8g", "16g", "32g")


def normalize_cpu(value, default) -> str:
    """Validate/normalize a CPU value against VALID_CPU.

    ``value`` is the template/config value (may be None/empty); ``default`` is
    the system fallback (already admin-validated). Returns the canonical string
    or raises ``ValueError`` with an actionable message.
    """
    cpu = str(value if value not in (None, "") else default).strip()
    if cpu not in VALID_CPU:
        raise ValueError(
            f"Invalid cpu '{cpu}': must be one of {', '.join(VALID_CPU)} "
            f"(integer processor count — not a fractional or Kubernetes-style value)"
        )
    return cpu


def normalize_memory(value, default) -> str:
    """Validate/normalize a memory value against VALID_MEMORY (Docker form).

    Case-folds so ``4G`` → ``4g``. Returns the canonical string or raises
    ``ValueError`` with an actionable message.
    """
    mem = str(value if value not in (None, "") else default).strip().lower()
    if mem not in VALID_MEMORY:
        raise ValueError(
            f"Invalid memory '{mem}': must be one of {', '.join(VALID_MEMORY)} "
            f"(Docker form like '4g' — not a Kubernetes-style value like '512Mi')"
        )
    return mem
