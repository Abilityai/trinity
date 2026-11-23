"""trinity-enterprise#1312 — the setup page is never logged and never leaks as Referer.

An emailed setup link may carry the setup code (and the email) in the query
string. The default nginx access log records the request line, and with
`Referrer-Policy: strict-origin-when-cross-origin` every same-origin follow-up
request (the view chunk, `/api/setup/status`, assets) carries the full URL as
`Referer`, which that log also records. So `/setup` gets its own location:

* `access_log off`;
* exactly one `Referrer-Policy: no-referrer`, through a rendered variant of the
  security-headers include with its Referrer-Policy line removed, so the page
  never sends two conflicting policies (nginx drops inherited `add_header` in a
  location that declares its own, so the include must be repeated there);
* `try_files /index.html =404`, which serves index.html IN this location.
  `try_files $uri /index.html` would end in an internal redirect to
  `location = /index.html`, which logs and sends the default policy;
* matches `/setup` and `/setup/` (Vue Router renders both).

The location lives in nginx-server.conf, which BOTH listeners in nginx.conf
include, so both are covered. The render script is executed, not read.
"""
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_FE = _ROOT / "src" / "frontend"
_RENDER = _FE / "render-nginx.sh"
_HEADERS = _FE / "security-headers.conf"
_SERVER = _FE / "nginx-server.conf"
_NGINX = _FE / "nginx.conf"
_NOREF_INCLUDE = "include /etc/nginx/generated/security-headers-noreferrer.conf;"


def _directives(block: str) -> list:
    lines = [ln.split("#", 1)[0].strip() for ln in block.splitlines()]
    return [ln for ln in lines if ln]


def _setup_block() -> str:
    body = _SERVER.read_text()
    m = re.search(r"^\s*location\s+~\s+\^/setup/\?\$\s*\{(.*?)^\s*\}", body, re.M | re.S)
    assert m, "nginx-server.conf has no `location ~ ^/setup/?$` block"
    return m.group(1)


def _render(tmp_path, env=None, headers_source=_HEADERS):
    template = tmp_path / "nginx.conf.template"
    template.write_text("server { set_real_ip_from ${TRINITY_PROXY_GATEWAY}; }\n")
    full_env = {
        "PATH": "/usr/bin:/bin", "TRINITY_PROXY_GATEWAY": "172.30.0.1",
        "TRINITY_NGINX_TEMPLATE": str(template), "TRINITY_NGINX_OUTPUT": str(tmp_path / "default.conf"),
        "TRINITY_HEADERS_SOURCE": str(headers_source),
        "TRINITY_HEADERS_OUTPUT": str(tmp_path / "security-headers.conf"),
        **(env or {}),
    }
    return subprocess.run(["sh", str(_RENDER), "true"], env=full_env, capture_output=True,
                          text=True, timeout=20)


def _header_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.strip().startswith("add_header")]


class TestTheSetupLocation:
    def test_the_regex_matches_setup_with_and_without_a_trailing_slash_only(self):
        pattern = re.compile(r"^/setup/?$")
        for uri in ("/setup", "/setup/"):
            assert pattern.match(uri)
        for uri in ("/setupx", "/setup/x", "/api/setup/status", "/x/setup"):
            assert not pattern.match(uri)

    def test_it_is_not_logged(self):
        assert "access_log off;" in _directives(_setup_block())

    def test_it_serves_index_html_in_place_not_by_internal_redirect(self):
        d = _directives(_setup_block())
        tries = [x for x in d if x.startswith("try_files")]
        assert tries == ["try_files /index.html =404;"]

    def test_it_sends_exactly_one_no_referrer_policy_and_every_other_header(self, tmp_path):
        done = _render(tmp_path)
        assert done.returncode == 0, done.stderr
        d = _directives(_setup_block())
        assert _NOREF_INCLUDE in d
        assert "include /etc/nginx/generated/security-headers.conf;" not in d, (
            "the default include would add a second Referrer-Policy"
        )
        effective = [x for x in d if x.startswith("add_header")]
        effective += _header_lines((tmp_path / "security-headers-noreferrer.conf").read_text())
        policies = [x for x in effective if x.split()[1] == "Referrer-Policy"]
        assert policies == ['add_header Referrer-Policy "no-referrer" always;']
        served = {x.split()[1] for x in effective}
        expected = {x.split()[1] for x in _header_lines(_HEADERS.read_text())}
        assert expected <= served, f"missing security headers: {expected - served}"

    def test_the_setup_page_is_not_cached(self):
        assert 'add_header Cache-Control "no-store" always;' in _directives(_setup_block())

    def test_both_listeners_include_the_shared_server_body(self):
        assert _NGINX.read_text().count("include /etc/nginx/nginx-server.conf;") == 2
        assert "location" not in "\n".join(_directives(_NGINX.read_text())), (
            "a location in nginx.conf would exist on one listener only"
        )


class TestTheRenderedNoReferrerInclude:
    def test_it_is_the_served_headers_minus_the_referrer_policy_line(self, tmp_path):
        done = _render(tmp_path)
        assert done.returncode == 0, done.stderr
        served = (tmp_path / "security-headers.conf").read_text().splitlines()
        noref = (tmp_path / "security-headers-noreferrer.conf").read_text().splitlines()
        assert noref == [ln for ln in served if not ln.strip().startswith("add_header Referrer-Policy")]
        assert len(served) - len(noref) == 1

    def test_it_follows_the_vendor_host_switches(self, tmp_path):
        done = _render(tmp_path, {"DO_NOT_TRACK": "1"})
        assert done.returncode == 0, done.stderr
        noref = (tmp_path / "security-headers-noreferrer.conf").read_text()
        assert "intake.abilityai.dev" not in noref

    def test_a_source_without_exactly_one_referrer_policy_refuses_to_start(self, tmp_path):
        bad = tmp_path / "src-headers.conf"
        bad.write_text("\n".join(ln for ln in _HEADERS.read_text().splitlines()
                                 if "Referrer-Policy" not in ln) + "\n")
        done = _render(tmp_path, headers_source=bad)
        assert done.returncode != 0
        assert "Referrer-Policy" in done.stderr

    def test_the_image_writes_it_where_nginx_reads_it(self):
        # The default output sits beside the served headers in the one directory
        # the unprivileged runtime user can write (Dockerfile.prod).
        script = _RENDER.read_text()
        assert "security-headers-noreferrer.conf" in script
        assert "/etc/nginx/generated/security-headers.conf" in script
