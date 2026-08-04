"""Registry transport + cache (trinity-enterprise#14).

Driven through a real `httpx` client over `MockTransport` — not a mocked
`get_registry_templates`. The properties here are transport properties (a byte
ceiling that survives a lying `Content-Length`, a redirect that is refused
rather than followed) and they cannot be proven against a stub of the function
that implements them.

Sync throughout: `tests/unit/pytest.ini` overrides `pyproject.toml`, so
`asyncio_mode = auto` does not apply in this directory and a bare
`async def test_*` would be collected and never awaited.
"""
import httpx
import pytest

import services.template_registry_service as trs

URL = "https://registry.example.com/registry.yaml"
GOOD = "version: 1\ntemplates:\n  - repo: acme/one\n    display_name: One\n"


class _Settings:
    """Stand-in for the settings singleton the service reads lazily."""

    def __init__(self):
        self.url = URL
        self.enabled = True
        self.generation = 1
        self.lkg = None

    def get_template_registry_url(self):
        return self.url

    def is_template_registry_enabled(self):
        return self.enabled

    def get_template_registry_generation(self):
        return self.generation

    def get_template_registry_lkg(self):
        return self.lkg

    def set_template_registry_lkg(self, payload):
        self.lkg = payload


@pytest.fixture
def env(monkeypatch):
    """Registry service with a controllable transport and settings.

    `_cache` is a module global; clearing it on both sides of every test is what
    keeps these independent under `pytest-randomly`.
    """
    trs.invalidate_registry_cache()
    settings = _Settings()

    import services.settings_service as ss
    monkeypatch.setattr(ss, "settings_service", settings, raising=True)

    # The SSRF gate resolves DNS. `registry.example.com` is not ours to resolve,
    # and a validator failure here would mask the transport assertions — so the
    # gate is proven separately (`test_url_validation_failure_degrades`) and
    # stubbed to a passthrough for the transport tests.
    monkeypatch.setattr(
        "utils.url_validation.validate_template_registry_url", lambda u: u
    )

    state = {"handler": None, "requests": []}

    def _client(timeout):
        def _dispatch(request):
            state["requests"].append(request)
            return state["handler"](request)

        return httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=httpx.MockTransport(_dispatch),
        )

    monkeypatch.setattr(trs, "_http_client", _client)

    class Env:
        settings = None
        def serve(self, handler):
            state["handler"] = handler
        def ok(self, body=GOOD):
            self.serve(lambda r: httpx.Response(200, text=body))
        @property
        def requests(self):
            return state["requests"]

    e = Env()
    e.settings = settings
    e.ok()
    yield e
    trs.invalidate_registry_cache()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_a_good_document_is_fetched_and_parsed(env):
    assert [t.repo for t in trs.get_registry_templates()] == ["acme/one"]
    status = trs.get_registry_status()
    assert status["last_status"] == "ok"
    assert status["template_count"] == 1
    assert status["stale"] is False
    assert status["last_error_code"] is None


def test_overrides_are_the_admin_override_shape(env):
    assert trs.get_registry_overrides() == [
        {"github_repo": "acme/one", "display_name": "One", "description": "", "priority": None}
    ]


# ---------------------------------------------------------------------------
# The byte ceiling — the load-bearing DoS bound
# ---------------------------------------------------------------------------

def test_an_oversize_body_is_refused(env):
    env.serve(lambda r: httpx.Response(200, text="#" * (trs.REGISTRY_MAX_BYTES + 1024)))
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_TOO_LARGE


def test_a_LYING_content_length_does_not_bypass_the_ceiling(env):
    """Content-Length is absent on chunked responses and trivially lied about,
    so it can never be the gate. The ceiling counts bytes ACTUALLY received."""
    huge = b"#" * (trs.REGISTRY_MAX_BYTES + 4096)

    def handler(request):
        return httpx.Response(
            200,
            headers=[("content-length", "12"), ("content-type", "text/yaml")],
            content=iter([huge[i:i + 8192] for i in range(0, len(huge), 8192)]),
        )

    env.serve(handler)
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_TOO_LARGE
    # Pin that the lie was actually told — otherwise a future refactor could
    # start trusting Content-Length and this test would still pass.
    assert env.requests, "the fetch never happened"
    with httpx.Client(transport=httpx.MockTransport(handler)) as probe:
        with probe.stream("GET", URL) as resp:
            assert resp.headers.get("content-length") == "12"
            assert sum(len(c) for c in resp.iter_bytes()) > trs.REGISTRY_MAX_BYTES


def test_a_declared_oversize_length_aborts_before_reading(env):
    """The early abort. Not the gate — the optimisation in front of it."""
    def handler(request):
        return httpx.Response(
            200,
            headers=[("content-length", str(trs.REGISTRY_MAX_BYTES * 100))],
            content=iter([b"x"]),
        )

    env.serve(handler)
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_TOO_LARGE


def test_a_body_just_under_the_ceiling_still_parses(env):
    padding = "#" * (trs.REGISTRY_MAX_BYTES - len(GOOD) - 16)
    env.serve(lambda r: httpx.Response(200, text=GOOD + padding))
    assert [t.repo for t in trs.get_registry_templates()] == ["acme/one"]


# ---------------------------------------------------------------------------
# Transport failure modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410, 429, 500, 502, 503])
def test_a_non_200_degrades(env, status_code):
    env.serve(lambda r: httpx.Response(status_code, text="nope"))
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_HTTP


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_not_followed(env, status_code):
    """A URL that passed the SSRF gate and then redirects is an SSRF bypass."""
    def handler(request):
        return httpx.Response(status_code, headers={"location": "http://169.254.169.254/"})

    env.serve(handler)
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_REDIRECT
    # One request only: the hop was never issued.
    assert len(env.requests) == 1


def test_a_connect_error_degrades(env):
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    env.serve(handler)
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_UNREACHABLE


def test_a_read_timeout_degrades(env):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    env.serve(handler)
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_TIMEOUT


def test_an_html_captive_portal_body_degrades(env):
    env.serve(lambda r: httpx.Response(200, text="<html><body>Sign in</body></html>"))
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] in (
        trs.ERROR_BAD_SHAPE, trs.ERROR_PARSE_REFUSED,
    )


def test_a_binary_body_degrades(env):
    env.serve(lambda r: httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\xff\xfe"))
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_BAD_SHAPE


def test_url_validation_failure_degrades(env, monkeypatch):
    def refuse(url):
        raise ValueError("Template registry URL must use HTTPS")

    monkeypatch.setattr("utils.url_validation.validate_template_registry_url", refuse)
    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_INVALID_URL
    assert env.requests == []  # never dialled


def test_the_status_never_carries_a_raw_exception_string(env):
    """A hostile server's response text must not reach the operator's panel."""
    marker = "ATTACKER-CONTROLLED-STRING"
    env.serve(lambda r: httpx.Response(500, text=marker))
    status = trs.get_registry_status()
    blob = repr(status)
    assert marker not in blob
    assert status["last_error_code"] in {
        trs.ERROR_DISABLED, trs.ERROR_INVALID_URL, trs.ERROR_UNREACHABLE,
        trs.ERROR_TIMEOUT, trs.ERROR_HTTP, trs.ERROR_REDIRECT, trs.ERROR_TOO_LARGE,
        trs.ERROR_PARSE_REFUSED, trs.ERROR_UNSUPPORTED_VERSION, trs.ERROR_BAD_SHAPE,
    }


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------

def test_disabled_makes_zero_requests(env):
    env.settings.enabled = False
    assert trs.get_registry_templates() == []
    assert env.requests == []
    assert trs.get_registry_status()["last_status"] == "disabled"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_a_second_call_within_the_ttl_makes_no_request(env):
    trs.get_registry_templates()
    trs.get_registry_templates()
    trs.get_registry_templates()
    assert len(env.requests) == 1


def test_an_expired_ttl_refetches(env, monkeypatch):
    trs.get_registry_templates()
    monkeypatch.setattr(trs, "REGISTRY_CACHE_TTL", 0.0)
    monkeypatch.setattr(trs, "REGISTRY_CACHE_JITTER", 0.0)
    trs._cache.next_check_at = 0.0
    trs.get_registry_templates()
    assert len(env.requests) == 2


def test_the_ttl_is_jittered_so_workers_do_not_realign(env):
    """A shared expiry instant is a correlated herd, not a rhythm."""
    assert trs.REGISTRY_CACHE_JITTER > 0
    trs.get_registry_templates()
    first = trs._cache.next_check_at
    trs.invalidate_registry_cache()
    trs.get_registry_templates()
    # Not a strict inequality assertion (jitter may collide); the property under
    # test is that the window is a RANGE, not a constant.
    assert trs._cache.next_check_at != first or trs.REGISTRY_CACHE_JITTER == 0


def test_a_url_change_discards_the_cache(env):
    trs.get_registry_templates()
    env.settings.url = "https://other.example.com/registry.yaml"
    trs.get_registry_templates()
    assert len(env.requests) == 2


def test_a_generation_bump_discards_the_cache_CROSS_WORKER(env):
    """The other uvicorn worker never runs `invalidate_registry_cache()`. Without
    the generation counter it keeps serving the old registry for a full hour,
    so an admin who repoints the URL sees it apply on half their page loads."""
    trs.get_registry_templates()
    assert len(env.requests) == 1
    env.settings.generation += 1          # what a settings write does
    trs.get_registry_templates()
    assert len(env.requests) == 2


def test_invalidate_clears_this_process(env):
    trs.get_registry_templates()
    trs.invalidate_registry_cache()
    trs.get_registry_templates()
    assert len(env.requests) == 2


# ---------------------------------------------------------------------------
# Serve-stale, bounded
# ---------------------------------------------------------------------------

def test_a_failed_refetch_serves_the_last_good_parse(env):
    assert [t.repo for t in trs.get_registry_templates()] == ["acme/one"]
    trs._cache.next_check_at = 0.0
    env.serve(lambda r: httpx.Response(500, text="down"))

    assert [t.repo for t in trs.get_registry_templates()] == ["acme/one"]
    status = trs.get_registry_status()
    assert status["stale"] is True
    assert status["last_status"] == "failed"
    assert status["last_error_code"] == trs.ERROR_HTTP


def test_stale_past_the_cap_degrades_to_the_floor(env):
    """Unbounded stale keeps a de-curated, renamed or compromised repo listed
    indefinitely while the operator sees a catalog that still renders."""
    import time as _time

    trs.get_registry_templates()
    trs._cache.next_check_at = 0.0
    trs._cache.fetched_at = _time.time() - (trs.REGISTRY_MAX_STALE_SECONDS + 60)
    env.settings.lkg = None
    env.serve(lambda r: httpx.Response(500, text="down"))

    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["stale"] is False


def test_a_failure_with_no_prior_good_parse_is_negative_cached(env, monkeypatch):
    env.serve(lambda r: httpx.Response(500, text="down"))
    trs.get_registry_templates()
    trs.get_registry_templates()
    trs.get_registry_templates()
    assert len(env.requests) == 1  # not one request per catalog load


# ---------------------------------------------------------------------------
# Durable last-known-good
# ---------------------------------------------------------------------------

def test_a_good_parse_is_persisted(env):
    trs.get_registry_templates()
    assert env.settings.lkg is not None
    assert env.settings.lkg["source_url"] == URL
    assert env.settings.lkg["entries"] == [
        {"repo": "acme/one", "display_name": "One", "description": "", "priority": None}
    ]
    assert env.settings.lkg["parser_version"] == trs.PARSER_VERSION
    assert env.settings.lkg["sha256"]


def test_unchanged_content_is_not_rewritten(env):
    trs.get_registry_templates()
    written = env.settings.lkg
    env.settings.set_template_registry_lkg = lambda payload: pytest.fail(
        "steady-state refetch of identical content must not write a row"
    )
    trs.invalidate_registry_cache()
    trs.get_registry_templates()
    assert env.settings.lkg is written


def test_a_cold_start_during_an_outage_serves_the_durable_copy(env):
    """The reason this ships in the same PR as default-ON: otherwise a first
    boot during a registry outage shows the operator the bundled floor — the
    exact first-screen problem the feature exists to fix, now with a network
    dependency in front of it."""
    trs.get_registry_templates()
    stored = env.settings.lkg
    trs.invalidate_registry_cache()          # a fresh worker
    env.serve(lambda r: httpx.Response(503, text="down"))
    env.settings.lkg = stored

    assert [t.repo for t in trs.get_registry_templates()] == ["acme/one"]
    assert trs.get_registry_status()["stale"] is True


def test_a_durable_copy_for_a_DIFFERENT_url_is_ignored(env):
    trs.get_registry_templates()
    stored = dict(env.settings.lkg, source_url="https://somewhere-else/registry.yaml")
    trs.invalidate_registry_cache()
    env.settings.lkg = stored
    env.serve(lambda r: httpx.Response(503, text="down"))
    assert trs.get_registry_templates() == []


def test_a_durable_copy_from_an_older_parser_is_ignored(env):
    trs.get_registry_templates()
    stored = dict(env.settings.lkg, parser_version=trs.PARSER_VERSION - 1)
    trs.invalidate_registry_cache()
    env.settings.lkg = stored
    env.serve(lambda r: httpx.Response(503, text="down"))
    assert trs.get_registry_templates() == []


def test_a_durable_copy_past_the_stale_cap_is_ignored(env):
    import time as _time

    trs.get_registry_templates()
    stored = dict(
        env.settings.lkg,
        fetched_at=_time.time() - (trs.REGISTRY_MAX_STALE_SECONDS + 60),
    )
    trs.invalidate_registry_cache()
    env.settings.lkg = stored
    env.serve(lambda r: httpx.Response(503, text="down"))
    assert trs.get_registry_templates() == []


def test_a_tampered_durable_copy_is_revalidated_on_read(env):
    """The row is ours, but it is reachable through the database. Validate on
    read as well as on write."""
    trs.get_registry_templates()
    stored = dict(env.settings.lkg)
    stored["entries"] = [
        {"repo": "../evil", "display_name": "x", "description": "", "priority": None},
        {"repo": "ok/one", "display_name": "y", "description": "", "priority": 3},
        {"repo": 12345, "display_name": "z", "description": "", "priority": None},
    ]
    trs.invalidate_registry_cache()
    env.settings.lkg = stored
    env.serve(lambda r: httpx.Response(503, text="down"))
    assert [t.repo for t in trs.get_registry_templates()] == ["ok/one"]


def test_an_unreadable_durable_row_is_not_fatal(env):
    trs.invalidate_registry_cache()
    env.settings.get_template_registry_lkg = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    env.serve(lambda r: httpx.Response(503, text="down"))
    assert trs.get_registry_templates() == []
