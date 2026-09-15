"""#2691 — the Public URL says what it does, and the tick is earned.

Three properties, each of which was false before this change:

1. **The ask gate canonicalises both sides.** SNI is ASCII, so Caddy asks about
   the A-label (`xn--…`) while an operator saves the name as they read it.
   Lower-casing alone left those unequal, so an internationalised domain could
   never obtain a certificate — silently, forever, on every visitor's page load.
2. **Reaching the gate is recorded.** It is the only evidence this instance can
   hold that the name actually works, and it survives a proxy, a load balancer
   or a reserved IP in between — which is exactly what a DNS lookup taken at
   save time does not.
3. **The save path refuses a value that cannot take effect**, before it
   re-points every Telegram webhook and WhatsApp binding at it.

The gate is exec-sliced rather than imported — the pattern `test_2380` already
uses on this handler, because importing the router pulls the whole backend graph
for what is a pure function over one settings read.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_PUBLIC_PY = _BACKEND / "routers" / "public.py"
_SETTINGS_PY = _BACKEND / "routers" / "settings.py"


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail


def _slice(src: str, marker: str) -> str:
    start = src.index(marker)
    return src[start : src.index("\n\n\n", start)]


def _request(headers: dict | None = None):
    """Only `.headers` is read, and header lookup is case-insensitive."""
    # `is None`, not truthiness: `{}` is a real case here — a caller with no
    # headers at all must fail closed, and `or` would have silently handed it
    # the loopback default and asserted nothing.
    default = {"host": "127.0.0.1:8000"}
    lowered = {k.lower(): v for k, v in (default if headers is None else headers).items()}
    return types.SimpleNamespace(headers=types.SimpleNamespace(get=lowered.get))


def _load_ask_gate(configured: str, latch=None):
    """The `tls_allowed` handler, with its settings read and latch stubbed.

    `_is_caddy_ask` is the REAL one — it decides whether a caller off the public
    internet can flip the stamp, so stubbing it would test nothing.
    """
    src = _PUBLIC_PY.read_text()
    ns: dict = {
        "asyncio": asyncio,
        "HTTPException": _HTTPException,
        "settings_service": types.SimpleNamespace(get_public_chat_url=lambda: configured),
        "_latch_public_url_reached": latch or (lambda host: None),
    }
    exec(_slice(src, "_ASK_HOSTS = "), ns)
    exec(_slice(src, "def _is_caddy_ask"), ns)
    exec(_slice(src, "async def tls_allowed"), ns)
    fn = ns["tls_allowed"]

    def call(domain: str, headers: dict | None = None):
        try:
            return asyncio.run(fn(_request(headers), domain=domain)), None
        except _HTTPException as e:
            return None, e.status_code

    return call


def _load_latch(stored: dict):
    """The `_latch_public_url_reached` helper, over an in-memory settings store."""
    src = _PUBLIC_PY.read_text()
    db = types.SimpleNamespace(
        get_setting_value=lambda key, default="": stored.get(key, default),
        set_setting=lambda key, value: stored.__setitem__(key, value),
    )
    ns: dict = {
        "db": db,
        "logger": types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None
        ),
        "PUBLIC_URL_REACHED_KEY": "public_url_reached_at",
        "_reached_memo": set(),
    }
    exec(_slice(src, "def _latch_public_url_reached"), ns)
    return ns["_latch_public_url_reached"]


# ---------------------------------------------------------------------------
# 1. Internationalised domains
# ---------------------------------------------------------------------------

def test_ask_gate_authorises_the_a_label_of_a_unicode_domain() -> None:
    """The operator saves `münchen.example.com`; Caddy asks about the A-label.

    Before #2691 these were compared as raw strings, so the gate refused its own
    configured name and the domain never obtained a certificate.
    """
    ask = _load_ask_gate("https://münchen.example.com")

    ok, status = ask("xn--mnchen-3ya.example.com")
    assert ok and ok["authorized"] is True, f"refused its own name (status {status})"
    # And the name it reports back is the one the wire actually uses.
    assert ok["domain"] == "xn--mnchen-3ya.example.com"


def test_ask_gate_accepts_the_unicode_spelling_too() -> None:
    """Whichever form arrives, both sides canonicalise to the same one."""
    ask = _load_ask_gate("https://xn--mnchen-3ya.example.com")
    ok, _ = ask("münchen.example.com")
    assert ok, "the same name in the other spelling was refused"


def test_canonicalisation_does_not_widen_the_allowlist() -> None:
    """The whole security model is exact-match; folding must not blur it."""
    ask = _load_ask_gate("https://münchen.example.com")
    for hostile in (
        "munchen.example.com",            # the confusable ASCII neighbour
        "xn--mnchen-3ya.example.com.evil",
        "example.com",
        "",
    ):
        ok, status = ask(hostile)
        assert ok is None and status == 404, f"issued for {hostile!r}"


# ---------------------------------------------------------------------------
# 2. The reachability latch
# ---------------------------------------------------------------------------

def test_reaching_the_gate_records_reachability() -> None:
    stored: dict = {}
    calls = []
    ask = _load_ask_gate("https://trinity.example.com", latch=calls.append)

    ok, _ = ask("trinity.example.com")
    assert ok, "the configured name was refused"
    assert calls == ["trinity.example.com"], "the authorised host was not recorded"

    _load_latch(stored)("trinity.example.com")
    stamp = stored["public_url_reached_at"]
    assert stamp.endswith("|trinity.example.com"), f"the host is not in the stamp: {stamp!r}"


def test_a_refused_name_records_nothing() -> None:
    """Otherwise anyone pointing DNS at this box would flip the tick."""
    calls = []
    ask = _load_ask_gate("https://trinity.example.com", latch=calls.append)
    for hostile in ("attacker.net", "trinity.example.com.evil", ""):
        ask(hostile)
    assert calls == [], "a refused handshake recorded reachability"


def test_a_request_off_the_public_internet_cannot_forge_the_stamp() -> None:
    """The gate answers on TWO paths: Caddy's local `ask`, and the public front
    door, because Caddy proxies everything to the frontend and nginx forwards
    `/api/` to the backend. The domain is published in every webhook URL, so
    without this `curl https://<ip>/api/public/tls-allowed?domain=…` would flip
    the instance to "domain reached" with no DNS record in existence.

    The gate must still ANSWER on that path — only the stamp is withheld.
    """
    calls = []
    ask = _load_ask_gate("https://trinity.example.com", latch=calls.append)

    ok, _ = ask(
        "trinity.example.com",
        # What nginx sends: the visitor's host, plus the forwarding headers it
        # always sets.
        {"host": "trinity.example.com", "x-forwarded-for": "203.0.113.9",
         "x-forwarded-proto": "https"},
    )
    assert ok and ok["authorized"] is True, "the gate must still authorise issuance"
    assert calls == [], "a public request forged the reachability stamp"

    # Fails closed on a shape it does not recognise, too.
    ask("trinity.example.com", {"host": "trinity.example.com"})
    ask("trinity.example.com", {})
    assert calls == [], "an unrecognised caller forged the reachability stamp"


def test_the_latch_writes_once_per_process() -> None:
    """Caddy re-asks on every renewal, on an unauthenticated handshake path, so
    the fast path must touch no database after the first hit."""
    stored: dict = {}
    latch = _load_latch(stored)
    latch("trinity.example.com")
    first = stored["public_url_reached_at"]
    stored.clear()
    latch("trinity.example.com")
    assert stored == {}, "a repeat handshake wrote again"
    assert first


def test_the_latch_never_raises() -> None:
    """It runs inside a TLS handshake. A settings write that fails must cost the
    stamp, never the operator's certificate."""
    src = _PUBLIC_PY.read_text()

    def _boom(*_a, **_k):
        raise RuntimeError("database is down")

    ns: dict = {
        "db": types.SimpleNamespace(get_setting_value=_boom, set_setting=_boom),
        "logger": types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None
        ),
        "PUBLIC_URL_REACHED_KEY": "public_url_reached_at",
        "_reached_memo": set(),
    }
    exec(_slice(src, "def _latch_public_url_reached"), ns)
    ns["_latch_public_url_reached"]("trinity.example.com")  # must not raise


# ---------------------------------------------------------------------------
# 3. The save path
# ---------------------------------------------------------------------------

def test_a_value_that_cannot_take_effect_is_refused() -> None:
    """`classify_advertised_url` is the grammar the refusal is written against —
    the same classifier the posture is derived from, not a second parser."""
    from services.settings_service import classify_advertised_url

    for junk in ("htp://typo.com", "just-a-hostname.com", "not a url", "://x"):
        assert classify_advertised_url(junk) == "unconfigured", f"{junk!r} would be stored"

    # Still legal: the managed fleet advertises plain HTTP behind a tunnel, and
    # clearing the setting must stay possible.
    assert classify_advertised_url("http://trinity.example.com") == "http"
    assert classify_advertised_url("https://trinity.example.com") == "https-domain"


def _put_public_url(monkeypatch, value: str):
    """Drive the real `update_setting` arm, with only its collaborators stubbed.

    Asserting on the source text (`"that string appears before this one"`) says
    nothing about what the handler DOES, and passes just as happily if the guard
    is present but unreachable.
    """
    import routers.settings as m

    written: dict = {}
    repointed: list = []

    async def _noop_audit(**_kwargs):
        return None

    async def _backfill(url):
        repointed.append(url)

    monkeypatch.setattr(m, "assert_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(m.db, "set_setting", lambda k, v: written.setdefault(k, v) or {"key": k, "value": v, "description": None, "updated_at": None})
    monkeypatch.setattr(m.platform_audit_service, "log", _noop_audit)
    monkeypatch.setattr(m, "_backfill_telegram_webhooks", _backfill)

    body = types.SimpleNamespace(value=value, description=None)
    request = types.SimpleNamespace(
        client=types.SimpleNamespace(host="127.0.0.1"),
        url=types.SimpleNamespace(path="/api/settings/public_chat_url"),
        state=types.SimpleNamespace(),
    )
    user = types.SimpleNamespace(id=1, username="admin", role="admin", mcp_scope=None, agent_name=None)

    status = None
    try:
        asyncio.run(m.update_setting("public_chat_url", body, request, user))
    except Exception as e:  # HTTPException from the real FastAPI
        status = getattr(e, "status_code", None)
        if status is None:
            raise
    return status, written, repointed


def test_the_save_path_refuses_a_value_that_cannot_take_effect(monkeypatch) -> None:
    """`htp://typo.com` used to store cleanly AND re-point every live binding."""
    status, written, repointed = _put_public_url(monkeypatch, "htp://typo.com")
    assert status == 422, f"stored junk (status {status})"
    assert written == {}, "the junk value was written"
    assert repointed == [], "webhooks were re-pointed at an address that answers nothing"


def test_the_save_path_still_accepts_what_live_installs_use(monkeypatch) -> None:
    """Plain HTTP is the managed fleet's own shape behind a tunnel, a trailing
    slash is normalised rather than refused, and clearing must stay possible."""
    status, written, repointed = _put_public_url(monkeypatch, "http://trinity.example.com")
    assert status is None and written["public_chat_url"] == "http://trinity.example.com"
    assert repointed == ["http://trinity.example.com"]

    status, written, _ = _put_public_url(monkeypatch, "https://trinity.example.com/")
    assert status is None and written["public_chat_url"] == "https://trinity.example.com"

    status, written, repointed = _put_public_url(monkeypatch, "")
    assert status is None and written["public_chat_url"] == ""
    assert repointed == [], "an empty value must not re-point anything"


def test_the_refusal_precedes_the_webhook_repoint() -> None:
    """Order is the whole point. A non-empty value re-registers every Telegram
    webhook and rewrites every WhatsApp binding; a warning that arrives after
    that arrives after the bots have already moved to a dead address."""
    src = _SETTINGS_PY.read_text()
    guard = src.index("That is not a URL Trinity can hand out")
    repoint = src.index("_backfill_telegram_webhooks(body.value)")
    assert guard < repoint, "validation runs after the webhooks are re-pointed"


def test_a_stamp_for_another_host_does_not_count(monkeypatch) -> None:
    """The stamp describes ONE name, and the reader compares it to the host in
    force. That is what makes a stale row — a restored backup, a direct edit, a
    writer that never learned to clear — read as not-reached rather than show a
    tick over a domain nobody has visited."""
    import services.settings_service as ss

    svc = ss.SettingsService()
    stamps: dict = {"public_url_reached_at": "2026-09-14T10:00:00Z|old.example.com"}
    monkeypatch.setattr(ss.db, "get_setting_value", lambda key, default="": stamps.get(key, default))
    svc.get_public_chat_url = lambda: "https://old.example.com"  # type: ignore[assignment]
    assert svc.is_public_url_reached() is True

    svc.get_public_chat_url = lambda: "https://new.example.com"  # type: ignore[assignment]
    assert svc.is_public_url_reached() is False, "a stale stamp showed a tick"

    # Cleared setting: nothing is in force, so nothing is reached.
    svc.get_public_chat_url = lambda: ""  # type: ignore[assignment]
    assert svc.is_public_url_reached() is False

    # And the same name in its other spelling still counts.
    stamps["public_url_reached_at"] = "2026-09-14T10:00:00Z|xn--mnchen-3ya.example.com"
    svc.get_public_chat_url = lambda: "https://münchen.example.com"  # type: ignore[assignment]
    assert svc.is_public_url_reached() is True


def test_reachability_is_on_the_flag_surface() -> None:
    """The first-run step and Settings both read it from there, and it must be a
    boolean — that surface reaches every authenticated principal."""
    src = _SETTINGS_PY.read_text()
    assert '"public_url_reached": settings_service.is_public_url_reached()' in src
