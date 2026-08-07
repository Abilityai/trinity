"""
Issue #848 — MCP_INLINE_AUTH_ENABLED must actually reach both containers.

The #848 review blocker: the flag was defined in ``config.py``, read in
``server.ts``, documented in ``.env.example``, gated at the router and covered by
a parametrized endpoint test — and wired into **zero** compose services. Compose
reads ``.env`` only for ``${...}`` interpolation, never to inject into a
container; with no ``env_file:`` and no Dockerfile ``ENV``, an operator setting
``MCP_INLINE_AUTH_ENABLED=true`` reached neither process. The whole feature was
permanently off with no lever: ``require_inline_auth_enabled`` 404s the internal
surface, the mcp-server refuses keyless connections and registers no auth tools,
and ``keyless_snippets`` never appears.

Why no existing signal caught it (and why this file has to exist):

* It fails **safe**, so nothing breaks — it just cannot be turned on.
* CI and ``/verify-local`` both boot at **defaults**, so a flag that can never be
  enabled boots clean and goes green. This class is invisible to every automated
  suite we have that does not read the compose files directly.
* ``grep MCP_INLINE_AUTH_ENABLED`` "finds" it — in ``.env.example`` and in the
  code — which reads as wired.

TWO processes read this one key, so it is **four** wirings: ``backend`` and
``mcp-server``, in ``docker-compose.yml`` and ``docker-compose.prod.yml``. The
backend read (``config.py``) gates ``/api/internal/mcp-auth/*`` and the keyless
connector snippet; the mcp-server read (``server.ts``) is the session-tier gate.
Half-wiring is the natural failure mode and produces a feature that is on in one
half and off in the other.

Static guard over the packaging surface — no Docker, no backend import (precedent:
``test_1489_vite_bug_build_args.py``, ``test_858_dockerfile_unbuffered.py``). It
asserts the *reader set* is covered, so adding a third process that reads the flag
without wiring it fails here.

Scope: this is the regression test for the #848 flag specifically, NOT a general
"every config.py flag is wired into compose" parity guard — most flags are
legitimately single-process or read via a different path, so the broad version
needs a curated allowlist and is its own piece of work.

Issue: https://github.com/Abilityai/trinity/issues/848
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/unit/ lives two levels under the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DEV = REPO_ROOT / "docker-compose.yml"
COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
BACKEND_CONFIG = REPO_ROOT / "src" / "backend" / "config.py"
MCP_SERVER_TS = REPO_ROOT / "src" / "mcp-server" / "src" / "server.ts"

FLAG = "MCP_INLINE_AUTH_ENABLED"
# The two services whose processes read FLAG. Keep in sync with the readers
# asserted by test_both_readers_still_read_the_flag below.
READER_SERVICES = ("backend", "mcp-server")
COMPOSE_FILES = (COMPOSE_DEV, COMPOSE_PROD)


def _service_environment_block(compose_text: str, service: str) -> str:
    """Return the raw ``environment:`` block text for one service.

    Deliberately a line-oriented scan rather than a YAML parse: these compose
    files carry the ``${VAR:-default}`` interpolation syntax we are asserting on,
    and a parse would either choke or normalise it away. We only need to know
    which literal env entries are declared under which service.
    """
    lines = compose_text.splitlines()

    # Find `  <service>:` at exactly 2-space indent (service keys under `services:`).
    svc_re = re.compile(rf"^  {re.escape(service)}:\s*$")
    start = next((i for i, ln in enumerate(lines) if svc_re.match(ln)), None)
    assert start is not None, f"service '{service}' not found in compose file"

    # The service block runs until the next 2-space-indented key.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]) and not lines[i].startswith("    "):
            end = i
            break
    block = lines[start:end]

    # Within it, take the `environment:` sub-block (4-space key).
    env_start = next(
        (i for i, ln in enumerate(block) if re.match(r"^    environment:\s*$", ln)),
        None,
    )
    assert env_start is not None, (
        f"service '{service}' has no 'environment:' block — if it switched to "
        f"env_file:, this test's assumption changed and needs revisiting"
    )
    env_end = len(block)
    for i in range(env_start + 1, len(block)):
        if re.match(r"^    \S", block[i]):
            env_end = i
            break
    return "\n".join(block[env_start:env_end])


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("service", READER_SERVICES)
def test_flag_is_wired_into_every_reader_service(compose_path: Path, service: str) -> None:
    """All four wirings: {backend, mcp-server} x {dev, prod} compose.

    Missing any one of these makes the feature un-switchable in that
    process/deployment combination — the #848 blocker.
    """
    env_block = _service_environment_block(compose_path.read_text(), service)
    assert FLAG in env_block, (
        f"{compose_path.name} service '{service}' does not declare {FLAG} in its "
        f"environment block. Compose reads .env only for ${{...}} interpolation, "
        f"never to inject into a container — so the operator's .env value never "
        f"reaches this process and the feature cannot be enabled. Copy the "
        f"sibling: - {FLAG}=${{{FLAG}:-false}}"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("service", READER_SERVICES)
def test_flag_defaults_to_false_and_is_operator_overridable(
    compose_path: Path, service: str
) -> None:
    """Must be ``${MCP_INLINE_AUTH_ENABLED:-false}``.

    Two properties in one assertion, both load-bearing:
      * ``${...}`` interpolation — a hardcoded ``=false`` would wire the key but
        pin it off, which is the same un-switchable bug with extra steps.
      * ``:-false`` default — this is a posture change on a network-exposed port
        (an unauthenticated MCP connection becomes possible), so an unset .env
        must reproduce pre-#848 behaviour exactly.
    """
    env_block = _service_environment_block(compose_path.read_text(), service)
    pattern = re.compile(
        rf"^\s*-\s*{re.escape(FLAG)}=\$\{{{re.escape(FLAG)}:-false\}}\s*$",
        re.MULTILINE,
    )
    assert pattern.search(env_block), (
        f"{compose_path.name} service '{service}' must declare exactly "
        f"'- {FLAG}=${{{FLAG}:-false}}'. A hardcoded value is not "
        f"operator-overridable; a default other than 'false' would make a "
        f"network-exposed keyless auth path opt-OUT instead of opt-in."
    )


def test_both_readers_still_read_the_flag() -> None:
    """Pins the reader set the wiring assertions are derived from.

    If a third process starts reading the flag, or one stops, this fails and
    forces READER_SERVICES to be reconsidered — otherwise the parametrization
    above would keep passing while silently under-covering the new reader.
    """
    assert FLAG in BACKEND_CONFIG.read_text(), (
        f"{FLAG} no longer read in src/backend/config.py — update READER_SERVICES"
    )
    assert FLAG in MCP_SERVER_TS.read_text(), (
        f"{FLAG} no longer read in src/mcp-server/src/server.ts — update READER_SERVICES"
    )


def test_flag_is_documented_in_env_example() -> None:
    """An operator cannot set a flag they cannot discover."""
    text = ENV_EXAMPLE.read_text()
    assert re.search(rf"^{re.escape(FLAG)}=", text, re.MULTILINE), (
        f"{FLAG} must appear as an assignment in .env.example so operators can "
        f"discover it"
    )


def test_inline_auth_timeout_is_wired_into_the_mcp_server() -> None:
    """``MCP_INLINE_AUTH_TIMEOUT_MS`` bounds the four backend relay fetches.

    mcp-server-only on purpose — the backend never reads this key, so asserting
    it on the backend service would encode a wiring that should not exist.
    Functional via its 15000 default, hence a lower-severity sibling of the flag
    blocker, but the same class: an inert lever.
    """
    key = "MCP_INLINE_AUTH_TIMEOUT_MS"
    client_ts = REPO_ROOT / "src" / "mcp-server" / "src" / "client.ts"
    assert key in client_ts.read_text(), f"{key} no longer read in client.ts"

    for compose_path in COMPOSE_FILES:
        env_block = _service_environment_block(compose_path.read_text(), "mcp-server")
        assert key in env_block, (
            f"{compose_path.name} service 'mcp-server' does not declare {key}; "
            f"the timeout override would be inert"
        )

    # And NOT on the backend, which does not read it.
    backend_src = REPO_ROOT / "src" / "backend"
    reads_in_backend = any(
        key in p.read_text(errors="ignore") for p in backend_src.rglob("*.py")
    )
    assert not reads_in_backend, (
        f"{key} is now read by the backend — wire it into the backend service in "
        f"both compose files and update this test"
    )
