"""Skills-library status must not disclose source repo URLs (ent#334).

`GET /api/skills/library/status` is `Depends(get_current_user)` — every
authenticated caller reaches it, agent-scoped MCP keys included, and that is
deliberate: the per-agent Skills tab and the MCP `get_skills_library_status`
tool both read it. But it returned `skill_service.get_library_status()` raw,
and that dict carries the source repo URLs — the exact value `GET
/api/skills/sources` is `require_admin` **plus** `reject_agent_principal` to
protect (ent#237 classes repo URLs as admin-sensitive; ent#293 added the second
gate because an agent-scoped key resolves to its owner *carrying the owner's
role*). So the open route handed out what the gated route withholds.

Two independent failure modes, and the tests separate them because the fixes
are different:

  * **the URL itself** — a private source's URL is sensitive as a *name*
    (org/repo discloses what the fleet is being taught from), and a persisted
    one may additionally carry `https://<token>@host/...` userinfo. The
    userinfo half is not hypothetical: `reject_embedded_credentials` guards
    only NEW writes, while rows written before it existed and rows written by
    the legacy-adoption path (which validates nothing) are still in the table.
  * **the field's presence** — asserting "the secret is not in the body" is
    not enough, because the org/repo variant leaks with no secret in it at all.
    Every route assertion here is on KEY ABSENCE.

The route-level fix is a `response_model` allow-list; the service-level fix is
`strip_url_credentials` at both emitters, so `GET /skills/sources` (which still
returns the URL, by design, to admins only) returns it userinfo-free and
`SkillSourcesPanel.vue` cannot render a token.

Test-credential convention: real prefix, obviously fake body
(`tok_placeholder`). `.gitleaks.toml` skips `tests/` pre-scan.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# =============================================================================
# strip_url_credentials — the never-raising, parse-based scrubber
# =============================================================================

class TestStripUrlCredentials:
    """Each case below is a shape one of the pre-existing scrubbers got wrong,
    or a shape that made a naive fix raise."""

    @pytest.mark.parametrize("raw, expected", [
        # The ordinary tokenized clone URL GitHub hands you for scripts.
        ("https://tok_placeholder@github.com/o/r", "https://github.com/o/r"),
        # Scheme-less shorthand — `validate_skills_library_url` accepts it, so
        # it reaches the table. `urlparse("tok@github.com/o/r").username` is
        # None (no authority without `//`), so a parse must assume a scheme —
        # but the RETURN must not invent one the caller never wrote.
        ("tok_placeholder@github.com/o/r", "github.com/o/r"),
        # user:password form.
        ("https://user:pw_placeholder@github.com/o/r", "https://github.com/o/r"),
        # DOUBLE `@`: the whole authority must go. Both pre-existing scrubbers
        # used `[^@]+@` / `[^@\s]+@`, which cannot cross the FIRST `@`, so the
        # second credential survived as `https://***@b@github.com/o/r`.
        ("https://a@b@github.com/o/r", "https://github.com/o/r"),
        # Empty userinfo — `parsed.username` is falsy here, so a
        # username-based test would leave the stray `@` in place.
        ("https://@github.com/x", "https://github.com/x"),
        # Protocol-relative. Caught by /review, not by the first cut: the
        # scheme test was `"://" in url`, false here, so the assumed scheme was
        # prepended to give `https:////tok@host` — whose netloc parses EMPTY,
        # so the `@`-in-netloc guard read it as clean and returned the token
        # VERBATIM. A leading `//` already carries an authority (RFC 3986) and
        # must never be prefixed. The frontend `stripUserinfo` lists this shape
        # explicitly; the two strips have to agree on it.
        ("//tok_placeholder@github.com/o/r", "//github.com/o/r"),
        # Non-http scheme, same authority rule.
        ("git+ssh://tok_placeholder@github.com/o/r", "git+ssh://github.com/o/r"),
    ])
    def test_userinfo_is_removed(self, raw, expected):
        from utils.url_validation import strip_url_credentials

        assert strip_url_credentials(raw) == expected

    def test_malformed_url_returns_instead_of_raising(self):
        """`urlparse` raises `ValueError: Invalid IPv6 URL` on an unbalanced
        bracket. Malformed rows are reachable — the legacy-adoption path writes
        a source with no validation at all — and the only caller is
        `get_library_status`, whose own comments say status must never 500 the
        panel. So the contract is *never raises*, not *usually doesn't*."""
        from utils.url_validation import strip_url_credentials

        import urllib.parse

        # Pin the premise: this really is a raising input, so the test cannot
        # silently become vacuous if CPython's parser gets more permissive.
        with pytest.raises(ValueError):
            urllib.parse.urlparse("https://[oops/repo").hostname

        assert strip_url_credentials("https://[oops/repo") == "https://[oops/repo"

    def test_malformed_url_with_credentials_still_gets_scrubbed(self):
        """The fallback for unparseable input is a scrub, not a passthrough —
        returning the input verbatim would leak on exactly the rows most likely
        to be malformed (the unvalidated ones)."""
        from utils.url_validation import strip_url_credentials

        out = strip_url_credentials("https://tok_placeholder@[oops/repo")
        assert "tok_placeholder" not in out

    @pytest.mark.parametrize("raw", [
        # An `@` in the QUERY is not userinfo. A bare `[^@]+@`-style regex
        # mangles this; deciding the authority by PARSING does not
        # (`skill_service._authenticated_url`'s house rule).
        "https://github.com/org/repo?ref=a@b",
        "https://github.com/owner/repo",
        "github.com/owner/repo",
        "owner/repo",
        "",
    ])
    def test_legitimate_urls_are_untouched(self, raw):
        from utils.url_validation import strip_url_credentials

        assert strip_url_credentials(raw) == raw

    def test_idempotent(self):
        """Both `get_library_status` emitters strip, and the flat one strips a
        value that came from an already-stripped dict."""
        from utils.url_validation import strip_url_credentials

        once = strip_url_credentials("https://tok_placeholder@github.com/o/r")
        assert strip_url_credentials(once) == once

    @pytest.mark.parametrize("raw", [None, 123, object()])
    def test_non_string_input_never_raises(self, raw):
        from utils.url_validation import strip_url_credentials

        strip_url_credentials(raw)  # no raise


# =============================================================================
# The public route
# =============================================================================

_SOURCE_ROW = {
    "id": "src_deadbeef",
    "name": "private-catalog",
    "url": "https://tok_placeholder@github.com/secretorg/secretrepo",
    "ref": "v1",
    "ref_type": "tag",
    "is_default": False,
    "enabled": True,
    "priority": 100,
    "cloned": True,
    "skills_root": "skills",
    "layout_conflict": False,
    "last_sync": "2026-08-06T00:00:00Z",
    "last_sync_status": "success",
    "commit_sha": "abc1234",
    "last_error": None,
    "skill_count": 4,
}

_STATUS = {
    "configured": True,
    "sources": [dict(_SOURCE_ROW)],
    "source_count": 1,
    "enabled_source_count": 1,
    "skill_count": 4,
    "multi_file_count": 1,
    "shadowed_count": 0,
    "last_sync": "2026-08-06T00:00:00Z",
    "last_sync_status": "success",
    "last_sync_error": None,
    "url": "https://tok_placeholder@github.com/secretorg/secretrepo",
    "branch": "v1",
    "cloned": True,
    "commit_sha": "abc1234",
}


def _client(monkeypatch, *, user):
    """A TestClient over the real skills router.

    A TestClient rather than calling the endpoint function directly: the
    `response_model` allow-list is applied by FastAPI's serialization layer, so
    the handler still returns the full dict and a direct call would prove
    nothing about what goes over the wire — the whole subject of this file.

    The overrides are keyed off ``skills_router.get_current_user``, NOT
    ``dependencies.get_current_user``. `tests/unit/conftest.py` pops
    `dependencies` from `sys.modules` after every test (`_POP_PREFIXES`), so a
    fresh `import dependencies` in the second test onward yields a *different*
    module object with a *different* function object, while `routers.skills`
    still holds the original. `dependency_overrides` is keyed by object
    identity, so the override would silently miss and every request would 401 —
    which is exactly how this was found. Always key off the symbol the router
    actually closed over.
    """
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import skills as skills_router
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    monkeypatch.setattr(
        skills_router.skill_service, "get_library_status", lambda: dict(_STATUS)
    )

    app = FastAPI()
    app.include_router(skills_router.router)
    app.dependency_overrides[skills_router.get_current_user] = lambda: user
    app.dependency_overrides[skills_router.require_admin] = lambda: user
    return TestClient(app)


def _user(role="admin", agent_name=None):
    from models import User

    return User(id=1, username="u", role=role, agent_name=agent_name)


class TestPublicStatusOmitsUrl:
    """KEY ABSENCE, not "the secret isn't in the body". The org/repo variant
    of this leak carries no secret at all, so a substring assertion passes on a
    payload that still discloses which private repo the fleet syncs from."""

    @pytest.mark.parametrize("role, agent_name, label", [
        ("admin", None, "admin"),
        ("user", None, "non-admin"),
        # An agent-scoped key resolves to its owner carrying the owner's role;
        # this route has no `reject_agent_principal` (deliberately — the Skills
        # tab and the MCP tool run as one), which is precisely why the payload
        # itself has to be safe.
        ("admin", "some-agent", "agent-scoped key on an admin-owned install"),
    ])
    def test_no_url_key_anywhere(self, monkeypatch, role, agent_name, label):
        client = _client(monkeypatch, user=_user(role=role, agent_name=agent_name))
        resp = client.get("/api/skills/library/status")

        assert resp.status_code == 200, label
        body = resp.json()
        assert "url" not in body, f"flat url leaked to {label}: {body}"
        for src in body.get("sources", []):
            assert "url" not in src, f"per-source url leaked to {label}: {src}"

    def test_flat_branch_and_commit_sha_are_deliberately_kept(self, monkeypatch):
        """Only the URL is the disclosure. `branch` is a ref name and
        `commit_sha` a commit hash — neither is a credential nor a repo
        identity, and the Library header renders both. Dropping them was
        considered and cut from this fix: it would be a second, unrelated
        behaviour change riding a security PR.
        """
        resp = _client(monkeypatch, user=_user()).get("/api/skills/library/status")

        # Status asserted first, always: a 401 body is `{"detail": ...}`, which
        # satisfies every "field not in body" assertion below. This test passed
        # vacuously against a broken client fixture before the assertion was
        # added — a key-absence test with no status check is not a test.
        assert resp.status_code == 200
        body = resp.json()
        assert "branch" in body
        assert "commit_sha" in body

    def test_load_bearing_fields_survive(self, monkeypatch):
        """`configured` and `cloned` drive the frontend stores' empty-state
        discriminator (`stores/skillsLibrary.js` → `emptyReason`,
        `stores/skills.js` gates on `configured`). Dropping either would turn a
        configured-but-unsynced library into a wrong "no library" state — the
        fix must not be paid for with a UX regression."""
        resp = _client(monkeypatch, user=_user()).get("/api/skills/library/status")
        assert resp.status_code == 200
        body = resp.json()

        for field in (
            "configured", "cloned", "skill_count", "source_count",
            "enabled_source_count", "multi_file_count", "shadowed_count",
            "last_sync", "last_sync_status", "last_sync_error", "sources",
        ):
            assert field in body, f"missing load-bearing field: {field}"
        assert body["configured"] is True
        assert body["cloned"] is True

    def test_per_source_non_url_fields_survive(self, monkeypatch):
        """Per-source `commit_sha` is kept — not a URL, and the panel reports
        per-source sync state."""
        resp = _client(monkeypatch, user=_user()).get("/api/skills/library/status")
        assert resp.status_code == 200
        src = resp.json()["sources"][0]

        for field in (
            "id", "name", "ref", "ref_type", "is_default", "enabled",
            "priority", "cloned", "skills_root", "layout_conflict",
            "last_sync", "last_sync_status", "commit_sha", "skill_count",
        ):
            assert field in src, f"missing per-source field: {field}"

    def test_per_source_last_error_is_withheld(self, monkeypatch):
        """`last_error` is git's failure text, which echoes the remote URL —
        and the clone path's URL carries a spliced PAT. `redact()` scrubs it
        on the way in but under-matches a double-`@` authority (ent#347),
        which is precisely the shape `_authenticated_url` builds when the
        stored URL already has userinfo — and that combination reliably fails,
        so the leaking branch is the guaranteed one.

        Withheld here, not everywhere: the admin-gated `GET /skills/sources`
        still returns it, which is where the operator who needs it looks.
        """
        resp = _client(monkeypatch, user=_user()).get("/api/skills/library/status")
        assert resp.status_code == 200
        assert "last_error" not in resp.json()["sources"][0]

    def test_admin_sources_route_still_returns_the_url(self, monkeypatch):
        """The admin-gated route is the supported place to see a source URL.
        This is the counterpart assertion: the fix must narrow the OPEN route,
        not blind the operator."""
        client = _client(monkeypatch, user=_user())
        resp = client.get("/api/skills/sources")

        assert resp.status_code == 200
        body = resp.json()
        assert "url" in body["sources"][0]


class TestSourcesRouteUrlIsUserinfoFree:
    """The admin route returns the URL; the SERVICE must have stripped it, so
    `SkillSourcesPanel.vue` (which renders `s.url`) cannot paint a token."""

    def test_service_strips_at_both_emitters(self, monkeypatch):
        import services.skill_service as ss

        service = ss.SkillService.__new__(ss.SkillService)
        row = SimpleNamespace(
            id="src_1",
            name="n",
            url="https://tok_placeholder@github.com/o/r",
            ref="main",
            ref_type="branch",
            is_default=False,
            enabled=True,
            priority=100,
            last_sync_at=None,
            last_sync_status="success",
            last_commit_sha="abc",
            last_error=None,
        )
        monkeypatch.setattr(ss.db, "list_skill_sources", lambda: [row], raising=False)
        monkeypatch.setattr(ss.db, "get_setting_value", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(ss.SkillService, "_clones", lambda self, enabled_only=False: [])
        monkeypatch.setattr(ss.SkillService, "list_skills", lambda self: [])
        service._last_sync = None

        status = service.get_library_status()

        assert "tok_placeholder" not in status["sources"][0]["url"]
        assert status["sources"][0]["url"] == "https://github.com/o/r"
        # The flat legacy field is a SECOND emitter of the same value; it is
        # dropped by the public response_model but still served, stripped, to
        # admins via GET /skills/sources.
        assert status["url"] == "https://github.com/o/r"

    def test_status_does_not_raise_on_a_malformed_stored_url(self, monkeypatch):
        """`_adopt_legacy_clone` writes a source row with no URL validation, so
        the table can hold input `urlparse` refuses. Status must degrade, not
        500 the panel (validating that write is ent#346, out of scope here)."""
        import services.skill_service as ss

        service = ss.SkillService.__new__(ss.SkillService)
        row = SimpleNamespace(
            id="src_1",
            name="n",
            url="https://[oops/repo",
            ref="main",
            ref_type="branch",
            is_default=False,
            enabled=True,
            priority=100,
            last_sync_at=None,
            last_sync_status=None,
            last_commit_sha=None,
            last_error=None,
        )
        monkeypatch.setattr(ss.db, "list_skill_sources", lambda: [row], raising=False)
        monkeypatch.setattr(ss.db, "get_setting_value", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(ss.SkillService, "_clones", lambda self, enabled_only=False: [])
        monkeypatch.setattr(ss.SkillService, "list_skills", lambda self: [])
        service._last_sync = None

        status = service.get_library_status()  # no raise

        assert status["sources"][0]["url"] == "https://[oops/repo"

    def test_unconfigured_flat_url_stays_none_not_empty_string(self, monkeypatch):
        """With no sources the flat field was `None`; coercing it to `""`
        through the scrubber would silently change what an unconfigured install
        reports to the admin route."""
        import services.skill_service as ss

        service = ss.SkillService.__new__(ss.SkillService)
        monkeypatch.setattr(ss.db, "list_skill_sources", lambda: [], raising=False)
        monkeypatch.setattr(ss.db, "get_setting_value", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(ss.SkillService, "_clones", lambda self, enabled_only=False: [])
        monkeypatch.setattr(ss.SkillService, "list_skills", lambda self: [])
        service._last_sync = None

        assert service.get_library_status()["url"] is None


# =============================================================================
# Static guard — the response_model must not be silently dropped
# =============================================================================

class TestResponseModelGuard:
    """The allow-list IS the fix. A refactor that drops `response_model=` from
    the decorator reopens the leak with no test failing anywhere else — the
    handler's return value is unchanged and every behavioural assertion above
    goes through FastAPI. So guard the declaration itself, statically.

    Read from DISK rather than `inspect.getsource(routers.skills)`: importing
    the router can be perturbed by another module's `sys.modules` stubs, and a
    static guard that silently stops running is worse than no guard
    (`test_ent237_skill_sources._router_source`'s rationale).
    """

    ROUTE_PATH = "/skills/library/status"

    def _decorator(self):
        src = (_BACKEND / "routers" / "skills.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if getattr(dec.func, "attr", "") != "get":
                    continue
                path = (
                    dec.args[0].value
                    if dec.args and isinstance(dec.args[0], ast.Constant)
                    else ""
                )
                if path == self.ROUTE_PATH:
                    return node.name, dec
        return None, None

    def test_route_exists_and_declares_a_response_model(self):
        name, dec = self._decorator()
        assert dec is not None, (
            f"GET {self.ROUTE_PATH} not found — was the route renamed? "
            "Update this guard rather than deleting it."
        )
        kwargs = {kw.arg: kw.value for kw in dec.keywords}
        assert "response_model" in kwargs, (
            f"GET {self.ROUTE_PATH} ({name}) has no response_model. It returns "
            "skill_service.get_library_status() raw, which carries "
            "admin-sensitive source repo URLs, on a route reachable by every "
            "authenticated caller including agent-scoped keys (ent#334)."
        )
        assert getattr(kwargs["response_model"], "id", None) == "SkillsLibraryStatus"

    def test_the_model_omits_url_by_construction(self):
        """Belt for the guard: even with the decorator intact, adding `url`
        back to the model would reopen the leak."""
        from models import SkillsLibraryStatus, SkillsLibrarySourceStatus

        assert "url" not in SkillsLibraryStatus.model_fields
        assert "url" not in SkillsLibrarySourceStatus.model_fields
        # `branch`/`commit_sha` are deliberately present — see the model
        # docstring. Not URLs, and the Library header renders them.
        assert "branch" in SkillsLibraryStatus.model_fields
        assert "commit_sha" in SkillsLibraryStatus.model_fields
        # The fields the frontend stores derive their empty state from.
        assert "configured" in SkillsLibraryStatus.model_fields
        assert "cloned" in SkillsLibraryStatus.model_fields
