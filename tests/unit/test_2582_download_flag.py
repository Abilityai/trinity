"""#2582 — `?download=1` is ONE-WAY, and a preview is not a download.

Two independent properties of `routers/files.py`, and the reason each is here.

**The flag's asymmetry.** ent#461 made `Content-Disposition` a server-decided
allowlist so a link opens inline on a phone; the AC asks for a requester-supplied
attachment flag, which the docstring on `_format_disposition` explicitly forbids.
Both can be true, but only in ONE direction: forcing `attachment` grants the
requester nothing (it is the strictly-safer disposition), while forcing `inline`
would be the stored XSS the allowlist exists to prevent. So the tests that matter
most here are not the ones proving `?download=1` works. They are the ones proving
it cannot be turned around — `?download=0` on `text/html` is still `attachment`,
and there is no parameter anywhere that spells `inline`.

**Tolerance, not validation.** The flag is `Optional[str]` with a truthy check
rather than `bool`, because a `bool` query param makes FastAPI answer **422** for
`?download=` or `?download=x` — on the PUBLIC link that Telegram, WhatsApp and an
iOS in-app browser open, and which today ignores every query pair it does not
recognise. A 422 there is a new outage mode traded for a type annotation.

**The counter.** The Workspace preview reads the head of a file, so its range
starts at byte 0 — indistinguishable, to the pre-#2582 code, from the start of a
real transfer. Every preview would therefore bump the owner's `download_count`
and write an audit row that reads like a download. The counter is now gated on a
FULL transfer while the audit row is kept and made separable (`ranged_prefix`),
so the two events stay tellable apart forever rather than one of them vanishing.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from routers.files import (  # noqa: E402
    _apply_download_flag,
    _format_disposition,
    _is_download_forced,
    is_inline_safe,
)


# --------------------------------------------------------------------------- #
# The parse — tolerant, and one-way
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " 1 ", "anything"])
def test_truthy_values_force_the_download(raw):
    assert _is_download_forced(raw) is True


@pytest.mark.parametrize("raw", [None, "", "  ", "0", "false", "no", "off", "FALSE"])
def test_absent_and_falsey_values_do_not(raw):
    assert _is_download_forced(raw) is False


def test_the_parse_is_total_over_strings():
    """No input spells "inline", and no input raises.

    This is the property the route depends on: `_is_download_forced` answers
    True or False for every string, so the handler has no error branch to get
    wrong and no third state to mean something.
    """
    for raw in ["inline", "attachment", "-1", "null", "1;DROP", "%00", "é"]:
        assert _is_download_forced(raw) in (True, False)


# --------------------------------------------------------------------------- #
# `_apply_download_flag` — the matrix
# --------------------------------------------------------------------------- #

def _headers(filename="report.pdf", inline=True):
    return {
        "Content-Disposition": _format_disposition(filename, inline=inline),
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Cache-Control": "private, max-age=3600",
    }


def test_the_flag_turns_inline_into_attachment():
    out = _apply_download_flag(_headers(inline=True), "report.pdf", "1")
    assert out["Content-Disposition"].startswith("attachment;")


def test_the_flag_cannot_turn_attachment_into_inline():
    """The whole point. `?download=0` on a type the allowlist rejects must stay
    `attachment` — there is no value of this parameter that relaxes anything."""
    base = _headers("evil.html", inline=is_inline_safe("text/html"))
    assert base["Content-Disposition"].startswith("attachment;")
    for raw in [None, "", "0", "false", "1", "inline", "yes"]:
        out = _apply_download_flag(base, "evil.html", raw)
        assert out["Content-Disposition"].startswith("attachment;"), raw


def test_an_absent_flag_returns_the_headers_unchanged():
    base = _headers()
    assert _apply_download_flag(base, "report.pdf", None) is base


def test_every_other_ent461_header_survives_the_flag():
    """`nosniff`, Range advertising, CORP and the cache window are each
    individually load-bearing on mobile (ent#461). The flag replaces exactly one
    key."""
    base = _headers()
    out = _apply_download_flag(base, "report.pdf", "1")
    for key in ("X-Content-Type-Options", "Accept-Ranges",
                "Cross-Origin-Resource-Policy", "Cache-Control"):
        assert out[key] == base[key], key
    assert out is not base, "must not mutate the caller's dict"


def test_the_filename_still_round_trips_rfc6266():
    out = _apply_download_flag(_headers(), "отчёт (1).pdf", "1")
    cd = out["Content-Disposition"]
    assert cd.startswith("attachment;")
    assert "filename*=UTF-8''" in cd


# --------------------------------------------------------------------------- #
# The route — through a real TestClient, both verbs
# --------------------------------------------------------------------------- #

def _mount(tmp_path, monkeypatch, *, mime, filename, stored):
    """Mount `/api/files` over exactly one share on disk, of a given type.

    Parametrized by type because the flag's contract has two halves that only
    differ by what `is_inline_safe` says about the row: an allowlisted type is
    `inline` bare and `attachment` when flagged, a rejected one is `attachment`
    no matter what anyone asks for.
    """
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import files as files_mod

    storage = tmp_path / "agent-files"
    storage.mkdir()
    (storage / stored).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1016)
    monkeypatch.setattr(files_mod, "STORAGE_ROOT", str(storage))

    row = {
        "id": "f1",
        "agent_name": "atlas",
        "filename": filename,
        "stored_filename": stored,
        "size_bytes": os.path.getsize(storage / stored),
        "mime_type": mime,
        "download_token": "tok",
        "created_at": "2026-09-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "revoked_at": None,
    }

    marked: list[str] = []
    audited: list[dict] = []

    class _DB:
        def get_agent_shared_file(self, file_id):
            return dict(row) if file_id == "f1" else None

        def mark_shared_file_downloaded(self, file_id):
            marked.append(file_id)

    async def _log(**kw):
        audited.append(kw)
        return "evt"

    monkeypatch.setattr(files_mod, "db", _DB())
    monkeypatch.setattr(files_mod.platform_audit_service, "log", _log)
    # The IP limiter reaches Redis; this route's behaviour under the limit is
    # not what is under test here.
    monkeypatch.setattr(files_mod, "_check_file_download_rate_limit", lambda ip: None)

    app = FastAPI()
    app.include_router(files_mod.router)
    c = TestClient(app)
    c._marked = marked
    c._audited = audited
    c._size = row["size_bytes"]
    return c


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A PNG share — `image/png` is IN the ent#461 inline allowlist, so it is the
    type whose bare response is `inline` and whose flagged response must be
    `attachment`: the defect the operator reported (an image "download" opening
    a tab)."""
    return _mount(tmp_path, monkeypatch, mime="image/png",
                  filename="chart.png", stored="stored.png")


@pytest.fixture()
def html_client(tmp_path, monkeypatch):
    """The other half of the matrix: `text/html` is NOT in the allowlist, and is
    the type where getting `inline` wrong is a stored-XSS delivery, not a
    cosmetic annoyance."""
    return _mount(tmp_path, monkeypatch, mime="text/html",
                  filename="evil.html", stored="stored.html")


def test_an_inline_safe_type_is_inline_bare_and_attachment_with_the_flag(client):
    bare = client.get("/api/files/f1?sig=tok")
    assert bare.status_code == 200
    assert bare.headers["content-disposition"].startswith("inline;")

    forced = client.get("/api/files/f1?sig=tok&download=1")
    assert forced.status_code == 200
    assert forced.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "1", "inline", "yes", "x"])
def test_a_rejected_type_stays_attachment_through_the_route_whatever_is_asked(html_client, raw):
    """The one-way property, proven where it is WIRED and not only where it is
    computed.

    `_apply_download_flag` is a pure function and is exercised directly above,
    but a pure function cannot tell you the route passed it the right
    `inline=`. The failure this catches is the plausible one: a handler that
    derives the disposition from the query parameter instead of from
    `is_inline_safe(row["mime_type"])`. On `text/html` that ships stored XSS on
    a public token-gated link, and it would pass every helper-level test in
    this file.
    """
    for verb in (html_client.get, html_client.head):
        res = verb(f"/api/files/f1?sig=tok&download={raw}")
        assert res.status_code == 200, f"{verb.__name__} download={raw!r}"
        assert res.headers["content-disposition"].startswith("attachment;"), \
            f"{verb.__name__} download={raw!r} -> {res.headers['content-disposition']}"


def test_a_rejected_type_is_attachment_with_no_flag_at_all(html_client):
    """The ent#461 baseline the flag must not regress: absent the parameter,
    a non-allowlisted type was already `attachment`."""
    res = html_client.get("/api/files/f1?sig=tok")
    assert res.headers["content-disposition"].startswith("attachment;")
    assert res.headers["x-content-type-options"] == "nosniff"


def test_head_agrees_with_get(client):
    """A client that HEADs and then GETs the same URL must be told the same
    disposition — otherwise it plans for one response and receives another."""
    assert client.head("/api/files/f1?sig=tok").headers["content-disposition"].startswith("inline;")
    assert client.head("/api/files/f1?sig=tok&download=1").headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("query", ["&download=", "&download=x", "&download=%00", "&download=1&download=2"])
def test_a_malformed_flag_never_422s(client, query):
    """The regression a `bool` annotation would have shipped, on the exact route
    ent#461 exists to keep opening from a phone."""
    for verb in (client.get, client.head):
        res = verb(f"/api/files/f1?sig=tok{query}")
        assert res.status_code == 200, f"{verb.__name__} {query} -> {res.status_code}"


def test_the_flag_survives_a_ranged_206(client):
    res = client.get("/api/files/f1?sig=tok&download=1", headers={"Range": "bytes=0-9"})
    assert res.status_code == 206
    assert res.headers["content-disposition"].startswith("attachment;")
    assert res.headers["accept-ranges"] == "bytes"


def test_the_flag_does_not_invalidate_the_sig(client):
    """`sig` is a STORED bearer token compared with `compare_digest`, not an HMAC
    over the URL — so appending a query pair cannot break it. Worth pinning
    because "the token signs the URL" is the obvious wrong assumption."""
    assert client.get("/api/files/f1?sig=tok&download=1").status_code == 200
    assert client.get("/api/files/f1?sig=wrong&download=1").status_code == 401


# --------------------------------------------------------------------------- #
# Preview is not a download
# --------------------------------------------------------------------------- #

def test_a_full_transfer_bumps_the_counter(client):
    client.get("/api/files/f1?sig=tok")
    assert client._marked == ["f1"]
    assert client._audited and client._audited[-1]["details"]["ranged_prefix"] is False


def test_a_ranged_prefix_read_is_audited_but_does_not_bump_the_counter(client):
    """The Workspace preview. Its range starts at byte 0, so before #2582 it was
    indistinguishable from the start of a real transfer and every preview
    inflated the owner's numbers."""
    res = client.get("/api/files/f1?sig=tok", headers={"Range": "bytes=0-99"})
    assert res.status_code == 206
    assert client._marked == [], "a partial read is not a download"
    assert len(client._audited) == 1, "but it is still audited — the event is not dropped"
    details = client._audited[-1]["details"]
    assert details["ranged_prefix"] is True
    assert details["ranged"] is True


def test_a_range_covering_the_whole_file_is_a_download(client):
    """A player asking for `bytes=0-` gets the whole file; that IS a transfer,
    and the boundary must be the byte count, not the presence of a header."""
    res = client.get(f"/api/files/f1?sig=tok", headers={"Range": f"bytes=0-{client._size - 1}"})
    assert res.status_code == 206
    assert client._marked == ["f1"]
    assert client._audited[-1]["details"]["ranged_prefix"] is False


def test_a_range_not_starting_at_zero_is_neither(client):
    """Unchanged ent#461 behaviour: a mid-file seek is one play continuing, so
    it neither counts nor audits."""
    res = client.get("/api/files/f1?sig=tok", headers={"Range": "bytes=100-199"})
    assert res.status_code == 206
    assert client._marked == []
    assert client._audited == []


def test_head_never_counts_or_audits(client):
    client.head("/api/files/f1?sig=tok")
    assert client._marked == []
    assert client._audited == []


# --------------------------------------------------------------------------- #
# The Files tab's URL carries the flag; the agent's chat link does not
# --------------------------------------------------------------------------- #

def test_portal_documents_appends_the_flag_and_build_download_url_does_not():
    """Two base URLs, one flagged. `portal_documents` builds the Workspace Files
    tab's link off `get_portal_base_url()`; `build_download_url` builds the
    agent's own chat link off `get_public_chat_url()`. Only the former gains the
    flag, which is what keeps ent#461's mobile inline path working."""
    import inspect
    from client_portal import service as portal_service
    from services import agent_shared_files_service

    portal_src = inspect.getsource(portal_service.portal_documents)
    assert "&download=1" in portal_src

    agent_src = inspect.getsource(agent_shared_files_service.build_download_url)
    assert "download=1" not in agent_src


@pytest.mark.parametrize('query', ['&preview=1', '&preview=true', '&preview=1&download=1'])
def test_full_blob_preview_is_audited_without_counting(client, query):
    res = client.get('/api/files/f1?sig=tok' + query)
    assert res.status_code == 200
    assert len(res.content) == client._size
    assert client._marked == []
    assert client._audited[-1]['details']['preview'] is True


def test_preview_preserves_auth_and_attachment_policy(client):
    assert client.get('/api/files/f1?sig=wrong&preview=1').status_code == 401
    res = client.get('/api/files/f1?sig=tok&preview=1&download=1')
    assert res.headers['content-disposition'].startswith('attachment;')


@pytest.mark.parametrize('query', ['', '&preview=0', '&preview=false', '&preview=x'])
def test_normal_download_still_counts_after_preview(client, query):
    client.get('/api/files/f1?sig=tok&preview=1')
    client._marked.clear()
    assert client.get('/api/files/f1?sig=tok&download=1' + query).status_code == 200
    assert client._marked == ['f1']


def test_preview_does_not_grant_inline_html(html_client):
    res = html_client.get('/api/files/f1?sig=tok&preview=1&download=0')
    assert res.status_code == 200
    assert res.headers['content-disposition'].startswith('attachment;')
    assert res.headers['x-content-type-options'] == 'nosniff'
    assert html_client._marked == []
