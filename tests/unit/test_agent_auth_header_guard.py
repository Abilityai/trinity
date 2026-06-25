"""Static guard: no unauthenticated backend→agent HTTP call (#1159).

Every backend code site that builds a raw ``http://agent-{name}:8000`` URL must
also be *auth-aware* — i.e. reference one of the agent-auth sending helpers
(``build_agent_auth_headers`` / ``agent_httpx_client`` / ``merge_auth_headers``)
— UNLESS every agent URL it builds targets the exempt ``/health`` probe. A new
caller that forgets the token shows up here as a raw URL with no helper.

This is the cheap backstop for the caller-completeness risk. It is deliberately
**file-level / coarse**: a file that authenticates one agent call but not a
second (and still references a helper) passes this guard. Per-call coverage is
the job of the positive route tests + `/verify-local` against a token-enabled
agent. The guard's value is catching a brand-new file (or a whole call site in a
file that previously had none) that bypasses the helper layer entirely.

Lives in tests/unit/ (not tests/) so it runs as a pure static check without the
backend-connection autouse fixtures the integration suite carries.
"""
import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# f-string form of the agent server host, e.g. f"http://agent-{name}:8000/...".
_RAW_AGENT_URL = re.compile(r"agent-\{[^}]+\}:8000")
# The same, restricted to the exempt /health probe.
_HEALTH_URL = re.compile(r"agent-\{[^}]+\}:8000/health(?:[\"'/]|$)")

# Files exempt because they ARE the helper layer the rest routes through.
_HELPER_LAYER = {
    "services/agent_auth.py",        # defines the helpers + the URL literal
    "services/agent_client.py",      # AgentClient._request injects via merge_auth_headers
    "services/agent_service/helpers.py",  # agent_http_request injects via merge_auth_headers
}

# References that prove a file stamps the auth header on its agent calls.
_AUTH_SENDING_HELPERS = (
    "build_agent_auth_headers",
    "agent_httpx_client",
    "merge_auth_headers",
)

# The scheduler tree ships its own (dead) client; excluded from this backend scan.
_EXCLUDE_DIRS = ("scheduler/",)


def _url_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if _RAW_AGENT_URL.search(ln)]


def _is_compliant(rel_path: str, text: str) -> bool:
    url_lines = _url_lines(text)
    if not url_lines:
        return True
    if rel_path in _HELPER_LAYER:
        return True
    if any(h in text for h in _AUTH_SENDING_HELPERS):
        return True
    # Pure health prober: every raw agent URL it builds is the exempt /health.
    return all(_HEALTH_URL.search(ln) for ln in url_lines)


def _scan_backend() -> list[str]:
    offenders = []
    for path in _BACKEND.rglob("*.py"):
        rel = path.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            continue
        text = path.read_text(encoding="utf-8")
        if not _is_compliant(rel, text):
            offenders.append(rel)
    return offenders


def test_no_unauthenticated_agent_calls():
    offenders = _scan_backend()
    assert not offenders, (
        "These backend files build a raw http://agent-{name}:8000 URL without "
        "referencing an agent-auth helper (build_agent_auth_headers / "
        "agent_httpx_client / merge_auth_headers) and are not pure /health "
        "probers (#1159). Route them through the helper layer:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_detects_planted_violation(tmp_path):
    """The guard must FAIL on a new caller that bypasses the helper layer."""
    bad = 'resp = await client.get(f"http://agent-{name}:8000/api/credentials/read")\n'
    assert _is_compliant("routers/planted_bad.py", bad) is False


def test_guard_passes_helper_aware_file():
    good = (
        "from services.agent_auth import build_agent_auth_headers\n"
        'resp = await client.get(f"http://agent-{name}:8000/api/x", '
        "headers=build_agent_auth_headers(name))\n"
    )
    assert _is_compliant("routers/planted_good.py", good) is True


def test_guard_passes_pure_health_prober():
    health = 'r = await client.get(f"http://agent-{name}:8000/health", timeout=2.0)\n'
    assert _is_compliant("services/planted_health.py", health) is True


def test_guard_flags_health_prober_that_also_hits_api():
    """A /health-only exemption must NOT cover a file that also hits /api/*."""
    mixed = (
        'await client.get(f"http://agent-{name}:8000/health")\n'
        'await client.get(f"http://agent-{name}:8000/api/credentials/read")\n'
    )
    assert _is_compliant("services/planted_mixed.py", mixed) is False


if __name__ == "__main__":  # pragma: no cover - manual scan helper
    found = _scan_backend()
    print("OFFENDERS:" if found else "CLEAN", found)
    raise SystemExit(1 if found else 0)
