"""Internal telemetry ports stay internal, and mail bodies stay out of the log
(abilityai/trinity-enterprise#1314).

Three properties, each one a line somebody could put back without noticing:

- no compose stack publishes Vector's API or the OTel Collector on the host;
- Vector's API listens on its container's loopback only;
- the backend never writes an email body to its log unless a developer asked.

The compose checks parse the files as YAML and look at every service, so a
port re-added under another service, or as `host:container/proto`, is caught
the same way.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

pytestmark = pytest.mark.unit

COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml"]
# Vector API; OTLP gRPC and HTTP; the Prometheus exporter; the collector's health port.
INTERNAL_PORTS = {8686, 4317, 4318, 8889, 13133}
INTERNAL_SERVICES = {"vector", "otel-collector"}


class _Loader(yaml.SafeLoader):
    """Compose files use anchors and merge keys; nothing else special."""


def _compose(name: str) -> dict:
    return yaml.load((_REPO / name).read_text(encoding="utf-8"), Loader=_Loader)


def _container_ports(entry) -> set[int]:
    """Every container-side port a `ports:` entry names, short or long syntax,
    ranges included."""
    if isinstance(entry, dict):
        target = entry.get("target")
        return {int(target)} if target is not None else set()
    text = str(entry).split("/")[0]
    # `[ip:][host:]container` — the container side is the last field. Variables
    # (`${X:-127.0.0.1}`) contain colons, so drop them before splitting.
    last = re.sub(r"\$\{[^}]*\}", "", text).split(":")[-1]
    if "-" in last:
        low, high = last.split("-", 1)
        return set(range(int(low), int(high) + 1))
    return {int(last)} if last.isdigit() else set()


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_no_stack_publishes_an_internal_telemetry_port(name):
    published = {}
    for service, spec in _compose(name)["services"].items():
        for entry in spec.get("ports") or []:
            hit = _container_ports(entry) & INTERNAL_PORTS
            if hit:
                published[service] = sorted(hit)
    assert published == {}, (
        f"{name} publishes internal telemetry ports on the host: {published}. "
        "Vector's API and the OTel Collector are reached inside Docker only.")


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_telemetry_services_publish_nothing_at_all(name):
    services = _compose(name)["services"]
    for service in INTERNAL_SERVICES:
        assert service in services, f"{name} has no `{service}` service: this guard is looking at nothing"
        assert not services[service].get("ports"), f"{name}: `{service}` must have no `ports:`"


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_vector_is_still_health_checked_from_inside_its_container(name):
    """Unpublishing the port must not have cost the healthcheck its target."""
    test = _compose(name)["services"]["vector"]["healthcheck"]["test"]
    assert "http://127.0.0.1:8686/health" in test


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_collector_stays_reachable_by_name_on_both_networks(name):
    """Agents push to it and the backend reads it, both by Docker DNS."""
    networks = _compose(name)["services"]["otel-collector"]["networks"]
    names = set(networks if isinstance(networks, list) else networks.keys())
    assert {"trinity-agent", "trinity-platform"} <= names


@pytest.mark.parametrize("config", ["config/vector.yaml", "config/vector.local.yaml"])
def test_vectors_api_listens_on_loopback_only(config):
    api = yaml.safe_load((_REPO / config).read_text(encoding="utf-8"))["api"]
    assert api["address"] == "127.0.0.1:8686", (
        f"{config}: the API must listen on the container's loopback, got {api['address']!r}")


def test_the_port_reader_understands_every_way_a_port_is_written():
    assert _container_ports("8686:8686") == {8686}
    assert _container_ports("127.0.0.1:8686:8686") == {8686}
    assert _container_ports("${BIND:-0.0.0.0}:9000:8686/tcp") == {8686}
    assert _container_ports("4317-4318:4317-4318") == {4317, 4318}
    assert _container_ports({"target": 8889, "published": "8889"}) == {8889}
    assert _container_ports("8000:8000") == {8000}


# ---------------------------------------------------------------------------
# Mail bodies
# ---------------------------------------------------------------------------

BODY = "Your code is 482913. It expires in 10 minutes."
CODE = "482913"


@pytest.fixture
def mail(monkeypatch, caplog, capsys):
    from services import email_service as module

    monkeypatch.delenv("EMAIL_CONSOLE_LOG_BODY", raising=False)
    service = module.EmailService()
    provider = {"name": "console", "resend_key": ""}
    monkeypatch.setattr(service, "_runtime_config", lambda: (provider["name"], provider["resend_key"], "noreply@example.com"))
    monkeypatch.setattr(module, "SMTP_HOST", "", raising=False)
    monkeypatch.setattr(module, "SMTP_USER", "", raising=False)
    monkeypatch.setattr(module, "SMTP_PASSWORD", "", raising=False)
    monkeypatch.setattr(module, "SENDGRID_API_KEY", "", raising=False)
    caplog.set_level(logging.DEBUG)

    def send(name="console"):
        provider["name"] = name
        caplog.clear()
        result = asyncio.run(service.send_email("person@example.com", "Your Trinity verification code", BODY))
        out = capsys.readouterr()
        return result, caplog.text + out.out + out.err

    return send


def test_console_mode_logs_who_and_what_but_never_the_body(mail):
    sent, logged = mail("console")
    assert sent is True
    assert "person@example.com" in logged and "Your Trinity verification code" in logged
    assert CODE not in logged


def test_a_developer_can_ask_for_the_body(mail, monkeypatch):
    monkeypatch.setenv("EMAIL_CONSOLE_LOG_BODY", "true")
    sent, logged = mail("console")
    assert sent is True and CODE in logged


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "maybe"])
def test_anything_but_a_clear_yes_keeps_the_body_out(mail, monkeypatch, value):
    monkeypatch.setenv("EMAIL_CONSOLE_LOG_BODY", value)
    _, logged = mail("console")
    assert CODE not in logged


@pytest.mark.parametrize("provider", ["smtp", "sendgrid", "resend", "postmark"])
def test_a_provider_that_cannot_send_says_so_and_prints_nothing(mail, monkeypatch, provider):
    """A provider missing its configuration used to print the whole mail and
    report it sent. The opt-in is for console mode; it does not apply here."""
    monkeypatch.setenv("EMAIL_CONSOLE_LOG_BODY", "true")
    sent, logged = mail(provider)
    assert sent is False, "an undelivered mail must not be reported as sent"
    assert CODE not in logged
    assert "not sent" in logged and "person@example.com" in logged


def test_a_verification_code_goes_through_the_same_door(monkeypatch, caplog, capsys):
    from services import email_service as module

    monkeypatch.delenv("EMAIL_CONSOLE_LOG_BODY", raising=False)
    service = module.EmailService()
    monkeypatch.setattr(service, "_runtime_config", lambda: ("console", "", "noreply@example.com"))
    monkeypatch.setattr(module.EmailService, "_relay_available", staticmethod(lambda: False))
    caplog.set_level(logging.DEBUG)
    assert asyncio.run(service.send_verification_code("person@example.com", CODE, context_label="Trinity login")) is True
    out = capsys.readouterr()
    assert CODE not in caplog.text + out.out + out.err


def _service(monkeypatch, provider):
    from services import email_service as module

    service = module.EmailService()
    monkeypatch.setattr(service, "_runtime_config", lambda: (provider, "", "noreply@example.com"))
    return service


@pytest.mark.parametrize("provider, flag, expected", [
    ("console", "true", True),      # a development install that asked for it
    ("console", "", False),         # console mode as shipped
    ("console", "false", False),
    ("resend", "true", False),      # the opt-in is for console mode only
    ("smtp", "true", False),
    ("postmark", "true", False),
])
def test_the_login_page_is_told_the_code_is_in_the_log_only_when_it_is(monkeypatch, provider, flag, expected):
    monkeypatch.setenv("EMAIL_CONSOLE_LOG_BODY", flag)
    assert _service(monkeypatch, provider).code_is_logged() is expected


def test_an_unreadable_config_never_claims_the_code_is_in_the_log(monkeypatch):
    from services import email_service as module

    service = module.EmailService()

    def broken():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(service, "_runtime_config", broken)
    monkeypatch.setenv("EMAIL_CONSOLE_LOG_BODY", "true")
    assert service.code_is_logged() is False


def test_what_the_flag_says_is_what_the_sender_does(monkeypatch, caplog, capsys):
    """The page's claim and the log's content come from the same two inputs."""
    caplog.set_level(logging.DEBUG)
    for provider, flag in (("console", "true"), ("console", ""), ("resend", "true")):
        monkeypatch.setenv("EMAIL_CONSOLE_LOG_BODY", flag)
        service = _service(monkeypatch, provider)
        caplog.clear()
        asyncio.run(service.send_email("person@example.com", "Your code", BODY))
        out = capsys.readouterr()
        in_log = CODE in caplog.text + out.out + out.err
        assert in_log is service.code_is_logged(), (provider, flag)


def test_the_auth_mode_response_carries_the_flag():
    source = (_REPO / "src/backend/routers/auth.py").read_text(encoding="utf-8")
    assert '"email_code_in_log": email_service.code_is_logged(),' in source


# ---------------------------------------------------------------------------
# Message content in info logs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, forbidden", [
    ("docker/base-image/agent_server/routers/chat.py", r"logger\.\w+\(.*request\.message\["),
    ("src/backend/adapters/message_router.py", r"logger\.\w+\(.*voice_text\["),
    ("src/backend/services/gemini_voice.py", r"logger\.\w+\(.*prompt\["),
    ("docker/base-image/agent_server/services/gemini_runtime.py", r"logger\.\w+\(.*response_text\["),
])
def test_these_log_lines_carry_a_length_not_the_words(path, forbidden):
    source = (_REPO / path).read_text(encoding="utf-8")
    assert not re.search(forbidden, source), f"{path} logs message content again"
