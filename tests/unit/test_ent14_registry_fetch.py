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


def _as_streaming(resp: httpx.Response) -> httpx.Response:
    """Rebuild a buffered mock response as a STREAMING one.

    `httpx.Response(text=...)` / `(content=...)` decodes and buffers in the
    CONSTRUCTOR and marks the stream consumed, so `iter_raw()` — the call the
    production path makes — raises `StreamConsumed` against it, and the buffered
    shape can only ever exercise `iter_bytes()`. That is precisely why the
    byte-ceiling tests were green while the ceiling was counting DECODED bytes
    (ent#14 S1): the harness could not express the failure it was asserting
    against. Converting centrally keeps every test's ordinary
    `httpx.Response(...)` spelling and still drives the real streaming path.

    A caller that needs a COMPRESSED body must build the streaming response
    itself (`stream=httpx.ByteStream(...)`) — passing compressed bytes as
    `content=` would decode them here, before the code under test ever sees
    them. That mistake is refused loudly rather than silently re-encoded.
    """
    if not resp.is_stream_consumed:
        return resp
    if "content-encoding" in resp.headers:
        raise AssertionError(
            "build a Content-Encoding response as a stream "
            "(httpx.Response(..., stream=httpx.ByteStream(raw))) — `content=` "
            "decodes it in the constructor, so the code under test would never "
            "see the compressed bytes"
        )
    return httpx.Response(
        resp.status_code, headers=resp.headers, stream=httpx.ByteStream(resp.content)
    )


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


def _install_settings_stub(monkeypatch, settings, *, github_templates=None):
    """Pin the stub by `sys.modules` KEY, not by module object.

    Both consumers resolve this module lazily, at call time:
    `template_service.get_all_templates` does
    `from services.settings_service import get_github_templates`, and
    `template_registry_service._resolve` / `_load_lkg` do
    `from services.settings_service import settings_service`. Both read
    `sys.modules["services.settings_service"]`.

    Patching an attribute on a separately-imported reference to that module is
    ORDER-FRAGILE — it passes in isolation and fails under the full run, where an
    earlier file has swapped the module object, so the real service is used
    instead. That failure is silent and confusing rather than loud: the real
    accessor reads the real (tmp) SQLite database, so a registry test asserting
    "degrades to the floor" gets the durable last-known-good a *previous* test
    legitimately persisted, and reports it as a fail-open bug that does not
    exist. `tests/unit/test_ent89_template_schedules.py` documents this trap;
    this is that lesson applied.

    The stub is built from a COPY of the real module's namespace, so anything
    else the test imports from it (`SettingsService`, key constants) still
    resolves.
    """
    import sys
    import types

    import services.settings_service as real

    fake = types.ModuleType("services.settings_service")
    fake.__dict__.update(real.__dict__)
    fake.settings_service = settings
    if github_templates is not None:
        fake.get_github_templates = github_templates
    monkeypatch.setitem(sys.modules, "services.settings_service", fake)
    return fake


@pytest.fixture
def env(monkeypatch):
    """Registry service with a controllable transport and settings.

    `_cache` is a module global; clearing it on both sides of every test is what
    keeps these independent under `pytest-randomly`.
    """
    trs.invalidate_registry_cache()
    settings = _Settings()

    _install_settings_stub(monkeypatch, settings)

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
            return _as_streaming(state["handler"](request))

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
# The ceiling counts WIRE bytes (ent#14 S1)
#
# The hole the tests above could not see: they asserted a DECODED total, and
# httpx sends `Accept-Encoding: gzip, deflate` by default, so a body whose wire
# size passes the `Content-Length` abort inflated ~1030:1 before the running
# total was consulted — 458 MB of transient allocation, on the event-loop
# thread, from a 199 KiB response. Correctness held (`too_large` was returned);
# the memory bound the ceiling exists to provide did not.
# ---------------------------------------------------------------------------

class _CountingStream(httpx.SyncByteStream):
    """A body that records how much of it was actually pulled off the wire.

    `read_bytes == 0` is the assertion that matters: a refusal that still reads
    the body is not a refusal, and a peak-memory assertion alone would not
    distinguish the two.
    """

    def __init__(self, payload: bytes, chunk: int = 64 * 1024):
        self._payload = payload
        self._chunk = chunk
        self.read_bytes = 0

    def __iter__(self):
        for i in range(0, len(self._payload), self._chunk):
            part = self._payload[i:i + self._chunk]
            self.read_bytes += len(part)
            yield part


def _gzip_bomb(decoded_mb: int = 64) -> tuple[bytes, int]:
    import gzip
    decoded = b"A" * (decoded_mb * 1024 * 1024)
    return gzip.compress(decoded, 9), len(decoded)


def test_the_request_asks_for_no_compression(env):
    """The polite half. It cannot bind a hostile server — that is what the
    refusal below is for — but a cooperative one should not compress ~1 KB of
    YAML in the first place."""
    trs.get_registry_templates()
    assert env.requests[0].headers.get("accept-encoding") == "identity"


def test_a_compressed_body_is_refused_WITHOUT_being_read(env):
    """A server that compressed anyway. The wire body is well under the ceiling,
    so the `Content-Length` abort passes it — and decoding it would allocate
    ~1030x its size before the running total is ever consulted."""
    wire, decoded_size = _gzip_bomb()
    assert len(wire) < trs.REGISTRY_MAX_BYTES, "the wire body must look legal"
    stream = _CountingStream(wire)

    env.serve(lambda r: httpx.Response(
        200,
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(wire))},
        stream=stream,
    ))

    assert trs.get_registry_templates() == []
    assert trs.get_registry_status()["last_error_code"] == trs.ERROR_ENCODING
    assert stream.read_bytes == 0, (
        "the body was read despite the encoding refusal — the header gate is "
        "meant to fire before a single byte is consumed"
    )
    assert decoded_size > trs.REGISTRY_MAX_BYTES * 100  # the bound that matters


def test_the_hostile_body_does_not_inflate_in_memory(env):
    """The empirical anchor of the finding, at 1/3 scale: 64 MB of decoded
    payload must not appear anywhere. Pre-fix this allocated the full decoded
    size; the threshold is deliberately generous so it measures the ORDER of
    magnitude, not an allocator detail."""
    import tracemalloc

    wire, decoded_size = _gzip_bomb()
    env.serve(lambda r: httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=httpx.ByteStream(wire),
    ))

    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        trs.get_registry_templates()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not already_tracing:
            tracemalloc.stop()

    assert peak < 8 * 1024 * 1024, (
        "peak %.1f MB from a %d-byte wire body — the ceiling is counting "
        "decoded bytes again" % (peak / 1e6, len(wire))
    )
    assert decoded_size == 64 * 1024 * 1024


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "GZIP", " gzip "])
def test_every_non_identity_encoding_is_refused(encoding):
    """Refused by NAME, not by whether httpx happens to ship a decoder for it —
    an encoding Trinity cannot decode would otherwise reach the UTF-8 decode and
    be reported as `bad_shape`, hiding the cause."""
    import services.template_registry_service as _trs

    def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": encoding},
                              stream=httpx.ByteStream(b"version: 1\ntemplates: []\n"))

    import httpx as _httpx
    original = _trs._http_client
    _trs._http_client = lambda t: _httpx.Client(
        timeout=t, follow_redirects=False, transport=_httpx.MockTransport(handler))
    try:
        text, err = _trs._fetch_registry_text(URL)
    finally:
        _trs._http_client = original
    assert (text, err) == (None, _trs.ERROR_ENCODING)


def test_an_explicit_identity_encoding_is_normal(env):
    """`Content-Encoding: identity` is what a compliant server echoes for an
    uncompressed body. Refusing it would break the honest case."""
    env.serve(lambda r: httpx.Response(
        200, headers={"Content-Encoding": "identity"},
        stream=httpx.ByteStream(GOOD.encode())))
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
