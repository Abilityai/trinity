"""Curated credential-path policy for CRED-002 injection (enterprise#11).

Single source of truth for *which* workspace paths may be written by the
user-facing credential-injection flow. Widens the original 3-path exact
allowlist (#183/#590/#598) to a curated set of credential file *types* — cloud
service-account JSON, TLS cert/key material, kubeconfigs, SSH keys — WITHOUT
reopening the arbitrary-path RCE surface the allowlist was built to close.

Policy model (deny takes precedence over allow):
  accepted  ==  (relative, no traversal)  AND  (matches an ALLOW rule)
                                          AND  (matches NO DENY rule)

Anything that is executed or sourced at shell/agent startup stays blocked
regardless of ALLOW (shell rc files, CLAUDE.md/AGENTS.md, .claude/**,
.mcp.json.template, .ssh/authorized_keys & config, .git/** & .gitconfig, bin/**).
`.mcp.json` passes the PATH layer here but is still CONTENT-validated by
`services.mcp_validator` (the #590/#598 guard) at the call site.

IMPORTANT: this module is vendored byte-identically into the agent base image at
``docker/base-image/agent_server/credential_paths.py`` (the agent server is a
separate image and cannot import ``src/backend``). The two copies MUST stay
byte-identical — a parity test enforces it (Invariant #5, defense in depth).
Keep this module pure-stdlib and free of repo-specific imports so the copy is
literally identical.
"""
from __future__ import annotations

import fnmatch

# Exact full-path matches (workspace root only).
ALLOW_EXACT = {".env", ".credentials.enc", ".mcp.json"}

# Glob rules. A pattern containing "/" is matched segment-by-segment ("**"
# matches one or more whole segments); a pattern with no "/" is matched against
# the path's BASENAME (so "*.pem" allows a .pem anywhere not under a denied dir).
ALLOW_GLOBS = [
    # cloud SDK ADC / service-account JSON. NOTE: a workload-identity ADC file
    # can declare an `executable` credential_source; google-auth only honors it
    # when GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=1 (non-default), so it is
    # not an injection→exec vector here — but don't set that env var fleet-wide.
    ".config/gcloud/**",
    ".kube/config",        # kubeconfig
    "*.pem", "*.key", "*.crt", "*.cert", "*.p12", "*.pfx",  # TLS / cert material
    ".ssh/id_*",           # SSH private/public keys (NOT authorized_keys/config)
]

# Deny rules — precedence over ALLOW. Nothing here may ever be written, even if
# it would match an ALLOW glob (e.g. a "*.key" placed under .ssh or .claude).
DENY_GLOBS = [
    # shell startup (sourced at login / non-interactive shells)
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
    ".zshrc", ".zprofile", ".zshenv", ".kshrc", ".cshrc",
    ".config/fish/**", ".config/systemd/**", ".config/environment.d/**",
    # agent / AI instruction + config (executed/interpreted at agent startup)
    "CLAUDE.md", "AGENTS.md", ".claude/**", ".mcp.json.template",
    # ssh login / command-execution vectors
    ".ssh/authorized_keys", ".ssh/config", ".ssh/known_hosts", ".ssh/environment",
    # git hook / fsmonitor command execution
    ".git/**", ".gitconfig",
    # anything on PATH
    "bin/**", ".local/bin/**",
    # vendored / dependency / cache trees — keeps the broad cert globs
    # (*.pem/*.key/*.crt) from matching bundled CA files (e.g. certifi's
    # cacert.pem) and sweeping them into export's `/list` walk (#11 live test).
    "node_modules/**", "**/node_modules/**",
    "site-packages/**", "**/site-packages/**",
    ".local/**", ".venv/**", "venv/**", ".cache/**", "go/pkg/**",
]


def _match_segs(pat: list[str], path: list[str]) -> bool:
    if not pat:
        return not path
    if pat[0] == "**":
        if len(pat) == 1:
            return len(path) >= 1          # "**" = one or more segments
        for i in range(1, len(path) + 1):
            if _match_segs(pat[1:], path[i:]):
                return True
        return False
    if not path:
        return False
    if not fnmatch.fnmatchcase(path[0], pat[0]):
        return False
    return _match_segs(pat[1:], path[1:])


def _matches(path: str, pattern: str) -> bool:
    if "/" in pattern:
        return _match_segs(pattern.split("/"), path.split("/"))
    # bare pattern → match the basename, so it applies anywhere in the tree
    return fnmatch.fnmatchcase(path.split("/")[-1], pattern)


def is_allowed_credential_path(path: str) -> bool:
    """True iff ``path`` (relative to the agent home) is a permitted credential
    file. Rejects absolute paths, traversal, and anything matching a DENY rule;
    accepts ALLOW_EXACT and ALLOW_GLOBS otherwise."""
    if not path or not isinstance(path, str):
        return False
    if "\x00" in path or "\\" in path:        # null byte / Windows separator
        return False
    if path.startswith("/"):                  # absolute
        return False
    segs = path.split("/")
    if any(s in ("", ".", "..") for s in segs):   # empty / "." / traversal
        return False
    if any(_matches(path, d) for d in DENY_GLOBS):  # deny precedence
        return False
    # `.ssh/` is high-risk: permit ONLY `id_*` key files — even a file that
    # would otherwise match `*.pem`/`*.key`. ssh never auto-loads a stray key,
    # but this keeps the most sensitive dir to exactly the intent ("SSH keys =
    # id_*") instead of "any cert-shaped file under .ssh" (#11 review).
    if segs[0] == ".ssh" and not fnmatch.fnmatchcase(segs[-1], "id_*"):
        return False
    if path in ALLOW_EXACT:
        return True
    return any(_matches(path, a) for a in ALLOW_GLOBS)


def disallowed_paths(paths) -> list[str]:
    """Return the subset of ``paths`` that are NOT permitted (empty = all ok)."""
    return [p for p in paths if not is_allowed_credential_path(p)]
