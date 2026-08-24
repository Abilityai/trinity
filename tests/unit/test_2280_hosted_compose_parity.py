"""Static guard: docker-compose.hosted.yml never drifts from prod (#2280).

``docker-compose.hosted.yml`` is the pull-only twin of ``docker-compose.prod.yml``.
It exists because every marketplace and managed-host channel (#2281 DigitalOcean,
#2282 Vultr, #2283 Hostinger/Dokploy) rejects a compose file that builds, and
because a fresh VM must not spend 5-10 minutes compiling the agent base image on
first boot.

Two compose files describing one platform is *exactly* the shape of the bug this
repo has now shipped five times and named: a knob is added to one file and never
reaches the other, so the .env variable is silently inert in the deployment that
matters. `LOG_*` (#1039), VoIP's master switch (#1056), `AGENT_AUTH_SECRET`
(#1707), the container log caps (#1871), and — as recently as #2381 —
``ADMIN_USERNAME``, which was present in the dev compose and absent from prod, so
an ``ADMIN_USERNAME=root`` install silently kept provisioning ``admin``.

So the hosted file is *generated* from prod and *guarded* here, rather than
maintained by hand and trusted. The rule this enforces:

    hosted == prod, MINUS every `build:` block, PLUS a GHCR `image:` for the
    four images Trinity builds. Nothing else may differ.

Deliberately compares the RAW yaml rather than ``docker compose config`` output:
the raw form still contains the unexpanded ``${VAR:-default}`` strings, so a
changed default (``VOIP_ENABLED:-false`` → ``:-true``) is a diff here, while the
resolved form would silently agree whenever the local .env happens to match.

Pure stdlib + PyYAML: no docker daemon, no backend import.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


_ROOT = Path(__file__).resolve().parents[2]
_PROD = _ROOT / "docker-compose.prod.yml"
_HOSTED = _ROOT / "docker-compose.hosted.yml"
_START_SH = _ROOT / "scripts" / "deploy" / "start.sh"

# The four images Trinity builds itself. Everything else in the file (redis,
# alpine, vector, cloudflared, otel-collector) is a third-party image that is
# already a pinned `image:` in prod and must stay byte-identical.
_BUILT_SERVICES = {
    "backend": "trinity-backend",
    "frontend": "trinity-frontend",
    "scheduler": "trinity-scheduler",
    "mcp-server": "trinity-mcp-server",
}

# The agent base image is deliberately NOT here: it is not a compose service in
# either file. The backend creates agent containers from the local tag
# `trinity-agent-base:latest` through the Docker SDK, so hosted mode pulls and
# retags it in start.sh instead. See test_start_sh_hosted_mode_pulls_agent_base.
_AGENT_BASE_TAG = "trinity-agent-base:latest"
_AGENT_BASE_REMOTE = "ghcr.io/abilityai/trinity-agent-base"


def _load(path: Path) -> dict:
    assert path.exists(), f"{path.name} is missing"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prod() -> dict:
    return _load(_PROD)


@pytest.fixture(scope="module")
def hosted() -> dict:
    return _load(_HOSTED)


def test_hosted_has_no_build_blocks(hosted: dict) -> None:
    """The whole point of the file. A `build:` block here fails every channel."""
    offenders = [
        name
        for name, svc in (hosted.get("services") or {}).items()
        if isinstance(svc, dict) and "build" in svc
    ]
    assert not offenders, (
        f"docker-compose.hosted.yml must be pull-only, but these services still "
        f"carry a build: block: {sorted(offenders)}. Managed hosts and template "
        f"catalogues reject compose files that build, and a marketplace droplet "
        f"that compiles on first boot fails the one-click bar (#2280)."
    )


def test_same_service_set(prod: dict, hosted: dict) -> None:
    assert set(hosted["services"]) == set(prod["services"]), (
        "hosted and prod describe different service sets — a service added to "
        "one file and not the other is the #1039/#1707 packaging gap."
    )


def test_built_services_resolve_ghcr_images(hosted: dict) -> None:
    for service, image_name in _BUILT_SERVICES.items():
        image = hosted["services"][service].get("image")
        assert image, f"hosted service '{service}' has no image: (and no build:)"
        assert image.startswith(f"ghcr.io/abilityai/{image_name}:"), (
            f"hosted '{service}' image is {image!r}; expected the published GHCR "
            f"copy ghcr.io/abilityai/{image_name}:..."
        )
        assert "${TRINITY_IMAGE_TAG" in image, (
            f"hosted '{service}' pins {image!r} with no TRINITY_IMAGE_TAG "
            f"indirection — an operator could then not pin a release without "
            f"editing the compose file."
        )


def test_third_party_images_identical(prod: dict, hosted: dict) -> None:
    """redis, vector, cloudflared, otel, alpine — must not drift independently."""
    for name, prod_svc in prod["services"].items():
        if name in _BUILT_SERVICES or "build" in prod_svc:
            continue
        assert hosted["services"][name].get("image") == prod_svc.get("image"), (
            f"third-party image for '{name}' differs between prod and hosted; "
            f"these are pinned in prod and must be inherited verbatim."
        )


@pytest.mark.parametrize("key", ["environment", "ports", "volumes", "networks",
                                 "depends_on", "container_name", "restart",
                                 "cap_drop", "cap_add", "security_opt", "user",
                                 "healthcheck", "command", "entrypoint",
                                 "group_add", "tmpfs", "sysctls", "extra_hosts"])
def test_service_key_parity(prod: dict, hosted: dict, key: str) -> None:
    """Every operational key is inherited verbatim.

    ``environment`` carries the weight: it IS the .env contract, and a variable
    present in prod but missing here is inert on exactly the installs that
    cannot debug it.
    """
    mismatches = []
    for name, prod_svc in prod["services"].items():
        hosted_svc = hosted["services"].get(name, {})
        if prod_svc.get(key) != hosted_svc.get(key):
            mismatches.append(name)
    assert not mismatches, (
        f"'{key}' differs between docker-compose.prod.yml and "
        f"docker-compose.hosted.yml for: {sorted(mismatches)}. Regenerate the "
        f"hosted file from prod rather than hand-editing it."
    )


def test_top_level_parity(prod: dict, hosted: dict) -> None:
    for key in ("volumes", "networks"):
        assert hosted.get(key) == prod.get(key), (
            f"top-level '{key}' differs between prod and hosted — named volumes "
            f"and the two-network isolation (#589) must be identical or a hosted "
            f"install silently loses data or puts agents on the platform network."
        )


def test_start_sh_hosted_mode_pulls_agent_base() -> None:
    """The agent base image is not a compose service, so `up` alone is not enough.

    A hosted install that skips this starts a platform which cannot create a
    single agent — and the failure surfaces later, at agent-create time, as a
    missing-image error rather than at install time.
    """
    text = _START_SH.read_text(encoding="utf-8")
    assert "--hosted" in text, "start.sh no longer offers --hosted (#2280)"
    assert "-f docker-compose.hosted.yml" in text, (
        "start.sh --hosted must select docker-compose.hosted.yml explicitly"
    )
    assert _AGENT_BASE_REMOTE in text, (
        f"start.sh --hosted must pull {_AGENT_BASE_REMOTE} — the backend creates "
        f"agents from the local tag {_AGENT_BASE_TAG} and compose cannot retag."
    )
    assert f"docker tag" in text and _AGENT_BASE_TAG in text, (
        f"start.sh --hosted must retag the pulled image as {_AGENT_BASE_TAG}: "
        f"that literal is hardcoded in services/agent_service/lifecycle.py and "
        f"allowlisted as 'trinity-agent-base:*' by SEC-172."
    )


def test_hosted_states_minimum_size() -> None:
    """AC6: the 8 GB floor is stated wherever the hosted compose is documented."""
    text = _HOSTED.read_text(encoding="utf-8")
    assert "8 GB" in text, (
        "docker-compose.hosted.yml must state the 8 GB RAM minimum — below it "
        "the agent containers and platform services contend (#2280 AC6)."
    )
