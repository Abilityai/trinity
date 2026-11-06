"""Path containment for the avatar store, agent rename, the create path's
staging files and local-template lookup (abilityai/trinity-enterprise#1117).

Every path these modules build from a caller-supplied agent or template name
goes through one barrier: normalise the base and the joined path, then require
the joined path to start with the base plus a trailing separator. The tests pin
the properties that barrier has to hold:

* a sibling directory that shares the base's prefix (``avatars2`` beside
  ``avatars``) is outside it, and so is any name carrying a NUL byte;
* a legitimate name is still served when the root is written with ``./``, with
  a doubled ``//`` separator, or sits behind a symlink;
* on the three unauthenticated read routes, an agent name carrying an encoded
  ``/``, ``..``, a backslash or a NUL cannot reach a file outside the store,
  and each route keeps its own 404 text and the WebP-before-PNG order;
* ``sanitize_agent_name`` never returns a separator, ``.`` or ``..``;
* ``a..b`` stays a valid agent name;
* a name longer than the filesystem allows gets the same answer on the four
  public read routes as an unknown agent, not a 500;
* every sink (read, write, move, staging) uses the normalised path the barrier
  checked, not one rebuilt from the raw root.
* the three staging names always get the same verdict, so a refused name
  leaves the staging root untouched.

It also pins two responses that used to carry exception text: the Generate
Default Avatars per-agent error, and the rename 500 (abilityai/trinity#4284).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import routers.agent_rename as agent_rename
import routers.avatar as avatar
from models import AgentConfig, AvatarGenerateRequest, User
from services import template_service
from services.agent_service import crud
from services.agent_service import rename as rename_service
from utils import avatar_paths
from utils.helpers import sanitize_agent_name

SECRET = b"SECRET-OUTSIDE-THE-AVATAR-STORE"


def _run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An avatar store under tmp, with a sibling ``avatars2`` holding files a
    traversal would reach. Every file outside the store carries SECRET."""
    root = tmp_path / "avatars"
    root.mkdir()
    sibling = tmp_path / "avatars2"
    sibling.mkdir()
    for name in ("x.webp", "x.png", "x_ref.png", "x_emotion_happy.webp", "x_emotion_happy.png"):
        (sibling / name).write_bytes(SECRET)
    for name in ("secret.webp", "secret.png", "secret_ref.png", "secret_emotion_happy.webp"):
        (tmp_path / name).write_bytes(SECRET)
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", root)
    return root


# =============================================================================
# 1. The barrier itself
# =============================================================================


@pytest.mark.parametrize("suffix", [".webp", ".png", "_ref.png", "_emotion_happy.webp"])
def test_sibling_prefix_escape_is_refused(store, suffix):
    """``<root>/../avatars2/x`` normalises to ``<root>2/x``, which starts with
    the string ``<root>``. Only a base with a trailing separator refuses it."""
    assert avatar_paths.avatar_path_or_none("../avatars2/x", suffix) is None
    with pytest.raises(HTTPException) as exc:
        avatar_paths.avatar_path("../avatars2/x", suffix)
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")


@pytest.mark.parametrize("name", ["a\x00b", "\x00", "x\x00/../../secret", "ok\x00"])
def test_nul_byte_is_refused(store, name):
    """A NUL survives normpath and the prefix check, so it is refused by name."""
    assert avatar_paths.avatar_path_or_none(name, ".webp") is None
    with pytest.raises(HTTPException) as exc:
        avatar_paths.avatar_path(name, ".webp")
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")


def test_escape_uses_the_callers_detail(store):
    with pytest.raises(HTTPException) as exc:
        avatar_paths.avatar_path("../secret", ".webp", detail="No avatar found")
    assert (exc.value.status_code, exc.value.detail) == (404, "No avatar found")


@pytest.mark.parametrize(
    "bad", ["../etc/passwd", "/abs/evil", "../../root", "../..", "a/../../b", "../avatars"]
)
def test_traversal_names_are_refused(store, bad):
    assert avatar_paths.avatar_path_or_none(bad, "_ref.png") is None


def _roots(tmp_path, monkeypatch):
    real = tmp_path / "real" / "avatars"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path)
    return real, {
        "plain": real,
        "dot-prefixed": "./real/avatars",
        "double-slash": f"{tmp_path}//real//avatars",
        "trailing-slash": f"{real}/",
        "symlinked": link / "avatars",
    }


@pytest.mark.parametrize("kind", ["plain", "dot-prefixed", "double-slash", "trailing-slash", "symlinked"])
@pytest.mark.parametrize("name", ["my-agent", "a..b", "my-agent_1.2", "9lives"])
def test_legitimate_names_resolve_under_every_root_spelling(tmp_path, monkeypatch, kind, name):
    """The base is normalised before the comparison; without that every
    legitimate name is refused under a ``./`` or ``//`` root."""
    real, roots = _roots(tmp_path, monkeypatch)
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", roots[kind])
    path = avatar_paths.avatar_path(name, ".webp")
    path.write_bytes(b"ok")
    assert (real / f"{name}.webp").read_bytes() == b"ok"


@pytest.mark.parametrize("kind", ["plain", "dot-prefixed", "double-slash", "symlinked"])
def test_legitimate_name_is_served_under_every_root_spelling(tmp_path, monkeypatch, kind):
    real, roots = _roots(tmp_path, monkeypatch)
    (real / "my-agent.webp").write_bytes(b"face")
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", roots[kind])
    resp = _run(avatar.get_avatar("my-agent"))
    assert isinstance(resp, FileResponse)
    assert Path(resp.path).read_bytes() == b"face"


def test_valid_name_builds_the_expected_path(store):
    assert avatar_paths.avatar_path("my-agent_1.2", "_ref.png") == store / "my-agent_1.2_ref.png"
    assert avatar_paths.avatar_path("a..b", ".webp") == store / "a..b.webp"


# =============================================================================
# 2. sanitize_agent_name never produces a path operator
# =============================================================================


@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.text())
def test_sanitize_agent_name_never_returns_a_separator_or_dot_segment(raw):
    out = sanitize_agent_name(raw)
    assert "/" not in out and "\\" not in out and "\x00" not in out
    assert out not in (".", "..")
    if out:
        assert out[0].isalnum()


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.sampled_from(list("./\\\x00-_aZ9 %2Fe")), max_size=24))
def test_sanitized_names_stay_direct_children_of_the_store(raw):
    out = sanitize_agent_name(raw)
    if not out:
        return
    root = Path("/data/avatars")
    path = avatar_paths.avatar_path(out, ".webp")
    assert path.parent == root and path.name == f"{out}.webp"


def test_dotted_names_survive_sanitization():
    assert sanitize_agent_name("a..b") == "a..b"


# =============================================================================
# 3. The read handlers go through the barrier (direct calls: a hostile name the
#    router's path matching would never hand them, so the barrier is the last line)
# =============================================================================


@pytest.mark.parametrize("name", ["../avatars2/x", "../secret", "a\x00b"])
def test_get_avatar_cannot_read_outside_the_store(store, name):
    with pytest.raises(HTTPException) as exc:
        _run(avatar.get_avatar(name))
    assert (exc.value.status_code, exc.value.detail) == (404, "No avatar found")


@pytest.mark.parametrize("name", ["../avatars2/x", "../secret", "a\x00b"])
def test_get_avatar_reference_cannot_read_outside_the_store(store, name):
    with pytest.raises(HTTPException) as exc:
        _run(avatar.get_avatar_reference(name))
    assert (exc.value.status_code, exc.value.detail) == (404, "No reference image found")


@pytest.mark.parametrize("name", ["../avatars2/x", "../secret", "a\x00b"])
def test_get_avatar_emotion_cannot_read_outside_the_store(store, name):
    with pytest.raises(HTTPException) as exc:
        _run(avatar.get_avatar_emotion(name, "happy"))
    assert (exc.value.status_code, exc.value.detail) == (404, "Emotion variant not found")


@pytest.mark.parametrize("name", ["../avatars2/x", "../secret", "a\x00b"])
def test_get_avatar_emotions_lists_nothing_outside_the_store(store, name):
    body = _run(avatar.get_avatar_emotions(name))
    assert body == {"agent_name": name, "emotions": [], "version": "0"}


@pytest.mark.parametrize("name", ["../avatars2/x", "../secret"])
def test_get_avatar_identity_sees_nothing_outside_the_store(store, monkeypatch, name):
    monkeypatch.setattr(avatar, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(avatar, "db", MagicMock(get_avatar_identity=MagicMock(return_value=None)))
    body = _run(avatar.get_avatar_identity(name, current_user=MagicMock()))
    assert body["has_avatar"] is False and body["has_reference"] is False


def test_read_handlers_keep_webp_before_png_and_their_404_texts(store):
    (store / "both.webp").write_bytes(b"W")
    (store / "both.png").write_bytes(b"P")
    (store / "pngonly.png").write_bytes(b"P")
    (store / "both_emotion_happy.webp").write_bytes(b"EW")
    (store / "both_emotion_happy.png").write_bytes(b"EP")
    (store / "both_ref.png").write_bytes(b"R")

    r = _run(avatar.get_avatar("both"))
    assert (r.media_type, Path(r.path).read_bytes()) == ("image/webp", b"W")
    r = _run(avatar.get_avatar("pngonly"))
    assert (r.media_type, Path(r.path).read_bytes()) == ("image/png", b"P")
    r = _run(avatar.get_avatar_emotion("both", "happy"))
    assert (r.media_type, Path(r.path).read_bytes()) == ("image/webp", b"EW")
    r = _run(avatar.get_avatar_reference("both"))
    assert Path(r.path).read_bytes() == b"R"
    assert _run(avatar.get_avatar_emotions("both"))["emotions"] == ["happy"]

    for call, detail in (
        (lambda: avatar.get_avatar("nobody"), "No avatar found"),
        (lambda: avatar.get_avatar_reference("nobody"), "No reference image found"),
        (lambda: avatar.get_avatar_emotion("nobody", "happy"), "Emotion variant not found"),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(call())
        assert (exc.value.status_code, exc.value.detail) == (404, detail)


# =============================================================================
# 4. The unauthenticated read routes over HTTP (the real router on a FastAPI app)
# =============================================================================


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(avatar.router)
    return TestClient(app)


_HOSTILE_SEGMENTS = [
    "..%2Favatars2%2Fx",
    "x%2F..%2F..%2Fsecret",
    "%2E%2E%2Fsecret",
    "%2e%2e",
    "..",
    "..%5Csecret",
    "a%5C..%5C..%5Csecret",
    "a%00b",
    "%00",
    "..%2F..%2F..%2F..%2F..%2F..%2Fetc%2Fpasswd",
    "%252E%252E%252Fsecret",
]
_ROUTES = ["/api/agents/{}/avatar", "/api/agents/{}/avatar/reference",
           "/api/agents/{}/avatar/emotion/happy", "/api/agents/{}/avatar/emotions"]


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("segment", _HOSTILE_SEGMENTS)
def test_hostile_agent_names_cannot_reach_files_outside_the_store(client, route, segment):
    resp = client.get(route.format(segment))
    assert SECRET not in resp.content
    if route.endswith("/emotions"):
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert resp.json()["emotions"] == []
    else:
        assert resp.status_code == 404


def test_legitimate_name_is_served_over_http(client, store):
    (store / "a..b.webp").write_bytes(b"face")
    resp = client.get("/api/agents/a..b/avatar")
    assert resp.status_code == 200 and resp.content == b"face"
    assert client.get("/api/agents/nobody/avatar").json() == {"detail": "No avatar found"}


# =============================================================================
# 5. One constant: every construction reads avatar_paths.AVATAR_DIR at call time
# =============================================================================


def _admin():
    return User(id=1, username="admin", role="admin")


def _defaults_env(monkeypatch, *, optimize):
    service = SimpleNamespace(
        available=True,
        generate_image=AsyncMock(return_value=SimpleNamespace(
            success=True, image_data=b"raw", error=None)),
    )
    db = MagicMock()
    db.get_agents_without_custom_avatar.return_value = [{"agent_name": "a1"}]
    db.get_avatar_identity.return_value = {"identity_prompt": "a robot"}
    monkeypatch.setattr(avatar, "get_image_generation_service", lambda: service)
    monkeypatch.setattr(avatar, "db", db)
    monkeypatch.setattr(avatar, "optimize_avatar", optimize)
    monkeypatch.setattr(
        "services.docker_service.list_all_agents_fast",
        lambda: [SimpleNamespace(name="a1")],
    )
    return db


def test_generate_defaults_writes_into_the_patched_store(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "avatars"
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", target)
    _defaults_env(monkeypatch, optimize=lambda b: b"WEBP")
    body = _run(avatar.generate_default_avatars(current_user=_admin()))
    assert body["generated"] == 1, body
    assert (target / "a1.webp").read_bytes() == b"WEBP"


def test_bundled_avatar_install_reads_the_same_constant(tmp_path, monkeypatch):
    """crud's install path has no constant of its own any more."""
    assert not hasattr(crud, "_AVATAR_DIR")
    assert crud.avatar_paths is avatar_paths


# =============================================================================
# 6. Agent rename moves the avatar files through the same barrier
# =============================================================================
# The route keeps the gates and the new-name checks; the moves happen in the
# rename primitive, `services/agent_service/rename.py::rename_agent_now`. Both
# modules' collaborators are faked, so these tests drive the real route into
# the real primitive.


def _rename_env(monkeypatch, *, rename_side_effect=None):
    container = MagicMock(status="exited")
    db = MagicMock()
    db.can_user_rename_agent.return_value = True
    db.is_system_agent.return_value = False
    db.is_volume_base_reserved.return_value = False
    db.rename_agent.return_value = True
    for module in (agent_rename, rename_service):
        monkeypatch.setattr(module, "db", db)
        monkeypatch.setattr(
            module, "get_agent_container",
            lambda name: None if name == "new-name" else container,
        )
    monkeypatch.setattr(rename_service, "container_stop", AsyncMock())
    monkeypatch.setattr(rename_service, "container_rename", AsyncMock(side_effect=rename_side_effect))
    monkeypatch.setattr(rename_service.platform_audit_service, "log", AsyncMock())
    monkeypatch.setattr("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock())
    monkeypatch.setattr("services.skill_gate_service.cancel_pending_for_agent", AsyncMock())
    monkeypatch.setattr(agent_rename, "manager", None)
    monkeypatch.setattr(agent_rename, "filtered_manager", None)
    user = User(id=2, username="alice", role="user")
    request = MagicMock()
    request.client.host = "127.0.0.1"
    request.scope = {"path": "/api/agents/x/rename"}
    return user, request


def test_rename_moves_avatar_files_inside_the_patched_store(store, monkeypatch):
    for name in ("old.webp", "old_ref.png", "old_emotion_happy.webp"):
        (store / name).write_bytes(b"F")
    user, request = _rename_env(monkeypatch)
    body = _run(agent_rename.rename_agent_endpoint(
        "old", SimpleNamespace(new_name="new-name"), request, user))
    assert body["new_name"] == "new-name"
    assert sorted(p.name for p in store.iterdir()) == [
        "new-name.webp", "new-name_emotion_happy.webp", "new-name_ref.png"]


def test_rename_reads_the_same_constant():
    """The rename primitive has no avatar constant or barrier of its own."""
    assert not hasattr(rename_service, "AVATAR_DIR")
    assert not hasattr(rename_service, "_avatar_path_or_none")
    assert rename_service.avatar_paths is avatar_paths


def test_rename_of_an_escaping_old_name_moves_nothing_outside_the_store(store, monkeypatch, tmp_path):
    user, request = _rename_env(monkeypatch)
    _run(agent_rename.rename_agent_endpoint(
        "../avatars2/x", SimpleNamespace(new_name="new-name"), request, user))
    assert (tmp_path / "avatars2" / "x.webp").read_bytes() == SECRET
    assert (tmp_path / "avatars2" / "x_ref.png").read_bytes() == SECRET
    assert list(store.iterdir()) == []


# =============================================================================
# 7. The create path's /tmp staging files
# =============================================================================


@pytest.fixture
def staging(tmp_path, monkeypatch):
    root = tmp_path / "stage"
    (root / "agent-x").mkdir(parents=True)
    monkeypatch.setattr(crud, "_STAGING_ROOT", str(root))
    return root


def test_staging_path_keeps_legitimate_names(staging):
    assert crud._staging_path("agent-my-agent-creds") == staging / "agent-my-agent-creds"
    assert crud._staging_path("agent-a..b.yaml") == staging / "agent-a..b.yaml"


@pytest.mark.parametrize("filename", [
    "agent-x/../../escaped-creds",
    "../escaped.yaml",
    "/etc/agent-x-credentials.json",
    "agent-a\x00b-creds",
    "agent-x/../../stage2/x",
])
def test_staging_path_refuses_an_escape(staging, filename):
    with pytest.raises(HTTPException) as exc:
        crud._staging_path(filename)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INVALID_AGENT_NAME"


def test_stage_config_files_writes_nothing_outside_the_staging_root(staging, tmp_path):
    config = AgentConfig(name="x/../../escaped")
    with pytest.raises(HTTPException) as exc:
        crud._stage_config_files(config, {}, None)
    assert exc.value.status_code == 400
    assert not any(p.name.startswith("escaped") for p in tmp_path.rglob("*"))


def test_stage_config_files_stages_a_legitimate_agent(staging):
    config_path, credentials_path, _, _ = crud._stage_config_files(
        AgentConfig(name="good-agent"), {}, None)
    assert config_path == staging / "agent-good-agent.yaml"
    assert credentials_path == staging / "agent-good-agent-credentials.json"
    assert (staging / "agent-good-agent-creds").is_dir()
    assert credentials_path.read_text() == "{}"


@pytest.mark.parametrize("kind", ["dot-prefixed", "double-slash", "symlinked"])
def test_staging_root_spellings_still_stage(tmp_path, monkeypatch, kind):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.chdir(tmp_path)
    root = {"dot-prefixed": "./real", "double-slash": f"{tmp_path}//real",
            "symlinked": str(tmp_path / "link")}[kind]
    monkeypatch.setattr(crud, "_STAGING_ROOT", root)
    crud._staging_path("agent-a-creds").mkdir()
    assert (real / "agent-a-creds").is_dir()


# =============================================================================
# 8. Local-template lookup
# =============================================================================


@pytest.fixture
def template_roots(tmp_path, monkeypatch):
    real = tmp_path / "real" / "templates"
    real.mkdir(parents=True)
    for name in ("sage", "dd-compliance", "a.b", "a_b", "9lives"):
        (real / name).mkdir()
    (tmp_path / "real" / "templates-evil").mkdir()
    (tmp_path / "outside").mkdir()
    (real / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "rootlink").symlink_to(tmp_path / "real")
    (tmp_path / "real" / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    # `Path` already folds `./` and a doubled inner `/`, but keeps a `..`
    # segment and a leading `//`: the spellings that need the base normalised.
    return real, {
        "plain": real,
        "dot-prefixed": Path("./real/templates"),
        "leading-double-slash": Path("/" + str(real)),
        "dot-dot-segment": tmp_path / "real" / "sub" / ".." / "templates",
        "symlinked": tmp_path / "rootlink" / "templates",
    }


@pytest.mark.parametrize(
    "kind", ["plain", "dot-prefixed", "leading-double-slash", "dot-dot-segment", "symlinked"])
def test_template_legitimate_names_resolve_under_every_root_spelling(template_roots, kind):
    real, roots = template_roots
    for name in ("sage", "dd-compliance", "a.b", "a_b", "9lives"):
        got = template_service.contained_template_dir(name, roots[kind])
        assert got == (real / name).resolve(), (kind, name)


@pytest.mark.parametrize(
    "kind", ["plain", "dot-prefixed", "leading-double-slash", "dot-dot-segment", "symlinked"])
def test_template_symlink_and_sibling_escapes_are_refused(template_roots, kind):
    _, roots = template_roots
    for name in ("link", "../templates-evil", "..", "../outside", "/etc", "a/b", "a\\b",
                 "a\x00b", "", ".hidden", "sage/../../outside"):
        assert template_service.contained_template_dir(name, roots[kind]) is None, (kind, name)


@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.one_of(
    st.text(),
    st.text(alphabet=st.sampled_from(list("./\\\x00-_aZ9link")), max_size=16),
))
def test_template_barrier_fuzz(template_roots, name):
    real, roots = template_roots
    root_resolved = real.resolve()
    for root in roots.values():
        got = template_service.contained_template_dir(name, root)
        if got is None:
            continue
        assert got.parent == root_resolved, (root, name, got)
        assert got.is_relative_to(root_resolved)
        assert got.name == name


# =============================================================================
# 9. Exception text stays out of responses (#1917 pattern)
# =============================================================================

SENTINEL = "SENTINEL-/data/avatars/private-path-and-internals"


def test_generate_defaults_error_carries_the_class_name_not_the_message(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", tmp_path / "avatars")

    def boom(_):
        raise RuntimeError(SENTINEL)

    _defaults_env(monkeypatch, optimize=boom)
    with caplog.at_level(logging.WARNING, logger=avatar.logger.name):
        body = _run(avatar.generate_default_avatars(current_user=_admin()))
    assert body["failed"] == 1
    err = body["errors"][0]
    assert err["agent"] == "a1"
    assert SENTINEL not in str(body)
    assert "RuntimeError" in err["error"]
    logged = [r for r in caplog.records if r.exc_info and SENTINEL in str(r.exc_info[1])]
    assert logged, "the full exception must still reach the backend log"


def test_rename_500_carries_the_class_name_not_the_message(store, monkeypatch, caplog):
    user, request = _rename_env(monkeypatch, rename_side_effect=RuntimeError(SENTINEL))
    with caplog.at_level(logging.ERROR, logger=agent_rename.logger.name):
        with pytest.raises(HTTPException) as exc:
            _run(agent_rename.rename_agent_endpoint(
                "old", SimpleNamespace(new_name="new-name"), request, user))
    assert exc.value.status_code == 500
    assert SENTINEL not in str(exc.value.detail)
    assert "RuntimeError" in exc.value.detail
    assert exc.value.detail.startswith("Failed to rename agent")
    logged = [r for r in caplog.records if r.exc_info and SENTINEL in str(r.exc_info[1])]
    assert logged, "the full exception must still reach the backend log"


# =============================================================================
# 10. Over-long names: refused like any other non-name, never a 500
# =============================================================================
# A path component longer than the filesystem allows makes `exists()` raise
# ENAMETOOLONG. The helper refuses it first, so the four public read routes give
# the over-long name exactly the answer they give any agent with no avatar.
# Bytes count, not characters: 128 two-byte letters are already over the limit.

_LONG_NAMES = ["a" * 240, "a" * 250, "a" * 256, "a" * 300, "a" * 5000, "é" * 128]


def test_a_component_of_exactly_255_bytes_is_kept_and_one_more_is_refused(store):
    name = "a" * (255 - len(".webp"))
    assert avatar_paths.avatar_path_or_none(name, ".webp") == store / f"{name}.webp"
    assert avatar_paths.avatar_path_or_none(name + "a", ".webp") is None
    # 128 two-byte letters: 128 characters, 256 bytes.
    assert avatar_paths.avatar_path_or_none("é" * 128, "") is None
    assert avatar_paths.avatar_path_or_none("é" * 127, "") is not None


@pytest.mark.parametrize("name", _LONG_NAMES, ids=lambda n: f"{len(n)}ch-{len(n.encode())}B")
def test_get_avatar_answers_an_over_long_name_like_an_unknown_agent(store, name):
    client = TestClient(_app_with_avatar_router(), raise_server_exceptions=False)
    resp = client.get(f"/api/agents/{name}/avatar")
    unknown = client.get("/api/agents/nobody/avatar")
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "No avatar found"}
    assert (resp.status_code, resp.json()) == (unknown.status_code, unknown.json())


@pytest.mark.parametrize("name", _LONG_NAMES, ids=lambda n: f"{len(n)}ch-{len(n.encode())}B")
def test_get_avatar_reference_answers_an_over_long_name_like_an_unknown_agent(store, name):
    client = TestClient(_app_with_avatar_router(), raise_server_exceptions=False)
    resp = client.get(f"/api/agents/{name}/avatar/reference")
    unknown = client.get("/api/agents/nobody/avatar/reference")
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "No reference image found"}
    assert (resp.status_code, resp.json()) == (unknown.status_code, unknown.json())


@pytest.mark.parametrize("name", _LONG_NAMES, ids=lambda n: f"{len(n)}ch-{len(n.encode())}B")
def test_get_avatar_emotion_answers_an_over_long_name_like_an_unknown_agent(store, name):
    client = TestClient(_app_with_avatar_router(), raise_server_exceptions=False)
    resp = client.get(f"/api/agents/{name}/avatar/emotion/happy")
    unknown = client.get("/api/agents/nobody/avatar/emotion/happy")
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "Emotion variant not found"}
    assert (resp.status_code, resp.json()) == (unknown.status_code, unknown.json())


@pytest.mark.parametrize("name", _LONG_NAMES, ids=lambda n: f"{len(n)}ch-{len(n.encode())}B")
def test_get_avatar_emotions_answers_an_over_long_name_like_an_unknown_agent(store, name):
    client = TestClient(_app_with_avatar_router(), raise_server_exceptions=False)
    resp = client.get(f"/api/agents/{name}/avatar/emotions")
    unknown = client.get("/api/agents/nobody/avatar/emotions")
    assert resp.status_code == 200, resp.text
    body, unknown_body = resp.json(), unknown.json()
    assert body.pop("agent_name") == name and unknown_body.pop("agent_name") == "nobody"
    assert body == unknown_body == {"emotions": [], "version": "0"}


def _app_with_avatar_router():
    app = FastAPI()
    app.include_router(avatar.router)
    return app


# =============================================================================
# 11. Every sink uses the path the barrier checked, not a re-built one
# =============================================================================
# The barrier decides on the NORMALISED path. A sink that rebuilds the path
# from the raw root (`AVATAR_DIR / f"{name}.webp"`) passes every test above,
# because the barrier calls beside it refuse a hostile name first. To make the
# difference observable, these tests use a root whose raw spelling and whose
# normalised spelling are two different directories on disk:
#
#     <tmp>/store/hop/../avatars     hop -> <tmp>/decoy/inner
#
# `normpath` folds `hop/..` as text, giving <tmp>/store/avatars (the CHECKED
# directory). The kernel follows the symlink first, so the raw spelling opens
# <tmp>/decoy/avatars. A sink that touches the decoy has touched a path the
# barrier never vouched for. No production root has this shape: it is a device
# that lets a test see which of the two paths a sink used.

CHECKED = b"THE-PATH-THE-BARRIER-CHECKED"


@pytest.fixture
def split_root(tmp_path):
    checked = tmp_path / "store" / "avatars"
    checked.mkdir(parents=True)
    decoy = tmp_path / "decoy" / "avatars"
    decoy.mkdir(parents=True)
    (tmp_path / "decoy" / "inner").mkdir()
    (tmp_path / "store" / "hop").symlink_to(tmp_path / "decoy" / "inner")
    raw = tmp_path / "store" / "hop" / ".." / "avatars"
    assert raw.resolve() == decoy.resolve()
    return SimpleNamespace(raw=raw, raw_str=str(raw), checked=checked, decoy=decoy)


def _plant(directory, names, data):
    for name in names:
        (directory / name).write_bytes(data)


_ALL_READ_FILES = ("x.webp", "x.png", "x_ref.png", "x_emotion_happy.webp", "x_emotion_happy.png")


def test_the_helper_returns_the_normalised_path_it_checked(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    got = avatar_paths.avatar_path_or_none("x", ".webp")
    assert got == split_root.checked / "x.webp"
    assert ".." not in got.parts


@pytest.mark.parametrize("route", ["avatar", "reference", "emotion-webp", "emotion-png"])
def test_read_routes_serve_the_checked_file_not_the_raw_spelling(split_root, monkeypatch, route):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _plant(split_root.decoy, _ALL_READ_FILES, SECRET)
    wanted = {
        "avatar": ("x.webp",),
        "reference": ("x_ref.png",),
        # Only one of the two variants exists in the checked store, so each
        # half of the WebP-then-PNG lookup is exercised on its own.
        "emotion-webp": ("x_emotion_happy.webp",),
        "emotion-png": ("x_emotion_happy.png",),
    }[route]
    _plant(split_root.checked, wanted, CHECKED)
    call = {
        "avatar": lambda: avatar.get_avatar("x"),
        "reference": lambda: avatar.get_avatar_reference("x"),
        "emotion-webp": lambda: avatar.get_avatar_emotion("x", "happy"),
        "emotion-png": lambda: avatar.get_avatar_emotion("x", "happy"),
    }[route]
    resp = _run(call())
    assert isinstance(resp, FileResponse)
    assert Path(resp.path).read_bytes() == CHECKED


def test_generate_defaults_writes_the_checked_path(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _defaults_env(monkeypatch, optimize=lambda b: CHECKED)
    body = _run(avatar.generate_default_avatars(current_user=_admin()))
    assert body["generated"] == 1, body
    assert (split_root.checked / "a1.webp").read_bytes() == CHECKED
    assert list(split_root.decoy.iterdir()) == []


def _generate_env(monkeypatch):
    service = SimpleNamespace(
        available=True,
        generate_image=AsyncMock(return_value=SimpleNamespace(
            success=True, image_data=b"PNG-BYTES", error=None, refined_prompt="r")),
    )
    monkeypatch.setattr(avatar, "get_image_generation_service", lambda: service)
    monkeypatch.setattr(avatar, "db", MagicMock())
    monkeypatch.setattr(avatar, "optimize_avatar", lambda b: b"WEBP-BYTES")
    monkeypatch.setattr(avatar, "_generate_emotions_background", AsyncMock())


def test_generate_writes_both_files_to_the_checked_path(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _generate_env(monkeypatch)
    _run(avatar.generate_avatar("x", AvatarGenerateRequest(identity_prompt="a robot")))
    assert (split_root.checked / "x.webp").read_bytes() == b"WEBP-BYTES"
    assert (split_root.checked / "x_ref.png").read_bytes() == b"PNG-BYTES"
    assert list(split_root.decoy.iterdir()) == []


def test_rename_moves_to_the_checked_path(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _plant(split_root.checked, ("old.webp", "old_ref.png"), CHECKED)
    user, request = _rename_env(monkeypatch)
    _run(agent_rename.rename_agent_endpoint(
        "old", SimpleNamespace(new_name="new-name"), request, user))
    assert sorted(p.name for p in split_root.checked.iterdir()) == [
        "new-name.webp", "new-name_ref.png"]
    assert list(split_root.decoy.iterdir()) == []


def test_bundled_avatar_install_writes_the_checked_path(split_root, monkeypatch, tmp_path):
    import io

    from PIL import Image

    tpl = tmp_path / "tpls" / "avtpl"
    tpl.mkdir(parents=True)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (40, 90, 160)).save(buf, format="PNG")
    (tpl / "avatar.png").write_bytes(buf.getvalue())
    monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (tmp_path / "tpls", tmp_path / "tpls"))
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    crud._install_bundled_avatar(AgentConfig(name="bundled-av", template="local:avtpl"))
    installed = (split_root.checked / "bundled-av.webp").read_bytes()
    assert installed[:4] == b"RIFF" and installed[8:12] == b"WEBP"
    assert list(split_root.decoy.iterdir()) == []


def test_stage_config_files_writes_the_checked_paths(split_root, monkeypatch):
    monkeypatch.setattr(crud, "_STAGING_ROOT", split_root.raw_str)
    config_path, credentials_path, _, _ = crud._stage_config_files(
        AgentConfig(name="good-agent"), {}, None)
    assert config_path.parent == credentials_path.parent == split_root.checked
    assert config_path.is_file() and credentials_path.read_text() == "{}"
    assert (split_root.checked / "agent-good-agent-creds").is_dir()
    assert list(split_root.decoy.iterdir()) == []


def test_move_avatar_file_treats_a_refused_name_as_nothing_to_move(store):
    """The contract its docstring states: a refused name is a quiet no-op. If it
    raised instead, the rename's broad `except` would abort the remaining moves."""
    (store / "old.webp").write_bytes(b"F")
    assert rename_service._move_avatar_file("../avatars2/x", "new", ".webp") is None
    assert rename_service._move_avatar_file("old", "../avatars2/x", ".webp") is None
    assert (store / "old.webp").read_bytes() == b"F"
    assert (store.parent / "avatars2" / "x.webp").read_bytes() == SECRET


def test_listing_and_identity_look_in_the_checked_store(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _plant(split_root.checked, ("x.webp", "x_ref.png", "x_emotion_calm.png"), CHECKED)
    _plant(split_root.decoy, ("x_emotion_happy.webp",), SECRET)
    assert _run(avatar.get_avatar_emotions("x"))["emotions"] == ["calm"]
    monkeypatch.setattr(avatar, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(avatar, "db", MagicMock(get_avatar_identity=MagicMock(return_value=None)))
    body = _run(avatar.get_avatar_identity("x", current_user=MagicMock()))
    assert body["has_avatar"] is True and body["has_reference"] is True


def test_generate_clears_stale_files_only_in_the_checked_store(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    stale = ("x.png", "x_emotion_happy.webp", "x_emotion_calm.png")
    _plant(split_root.checked, stale, CHECKED)
    _plant(split_root.decoy, stale, SECRET)
    _generate_env(monkeypatch)
    _run(avatar.generate_avatar("x", AvatarGenerateRequest(identity_prompt="a robot")))
    assert sorted(p.name for p in split_root.checked.iterdir()) == ["x.webp", "x_ref.png"]
    assert sorted(p.name for p in split_root.decoy.iterdir()) == sorted(stale)


def test_regenerate_reads_and_writes_the_checked_path(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _plant(split_root.checked, ("x_ref.png",), CHECKED)
    service = SimpleNamespace(
        available=True,
        generate_variation=AsyncMock(return_value=SimpleNamespace(
            success=True, image_data=b"VARIATION", error=None)),
    )
    monkeypatch.setattr(avatar, "get_image_generation_service", lambda: service)
    monkeypatch.setattr(avatar, "db", MagicMock(
        get_avatar_identity=MagicMock(return_value={"identity_prompt": "a robot"})))
    monkeypatch.setattr(avatar, "optimize_avatar", lambda b: b"WEBP-" + b)
    _run(avatar.regenerate_avatar("x"))
    assert service.generate_variation.await_args.kwargs["reference_image"] == CHECKED
    assert (split_root.checked / "x.webp").read_bytes() == b"WEBP-VARIATION"
    assert list(split_root.decoy.iterdir()) == []


def test_background_emotions_read_and_write_the_checked_path(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _plant(split_root.checked, ("x_ref.png",), CHECKED)
    service = SimpleNamespace(generate_emotion_variation=AsyncMock(return_value=SimpleNamespace(
        success=True, image_data=b"EMO", error=None)))
    monkeypatch.setattr(avatar, "get_image_generation_service", lambda: service)
    monkeypatch.setattr(avatar, "optimize_avatar", lambda b: b"WEBP-" + b)
    _run(avatar._generate_emotions_background("x", CHECKED, "a robot"))
    written = sorted(p.name for p in split_root.checked.iterdir() if "_emotion_" in p.name)
    assert written == sorted(f"x_emotion_{e}.webp" for e in avatar.AVATAR_EMOTIONS)
    assert list(split_root.decoy.iterdir()) == []


def test_delete_removes_only_the_checked_files(split_root, monkeypatch):
    monkeypatch.setattr(avatar_paths, "AVATAR_DIR", split_root.raw)
    _plant(split_root.checked, _ALL_READ_FILES, CHECKED)
    _plant(split_root.decoy, _ALL_READ_FILES, SECRET)
    monkeypatch.setattr(avatar, "db", MagicMock())
    _run(avatar.delete_avatar("x"))
    assert list(split_root.checked.iterdir()) == []
    assert sorted(p.name for p in split_root.decoy.iterdir()) == sorted(_ALL_READ_FILES)


# =============================================================================
# 12. Hostile names through the write paths (characterization)
# =============================================================================
# These callers take names that are owner-gated, read from the database or
# sanitised, so none is reachable with a hostile name today; the tests pin that
# a hostile name writes nothing, should a future caller pass one.


def test_generate_refuses_an_escaping_name_and_writes_nothing(store, monkeypatch):
    _generate_env(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(avatar.generate_avatar("../avatars2/x", AvatarGenerateRequest(identity_prompt="a")))
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
    assert (store.parent / "avatars2" / "x.webp").read_bytes() == SECRET
    assert (store.parent / "avatars2" / "x_ref.png").read_bytes() == SECRET
    assert list(store.iterdir()) == []


def test_generate_defaults_writes_nothing_for_an_escaping_database_name(store, monkeypatch):
    db = _defaults_env(monkeypatch, optimize=lambda b: b"WEBP")
    db.get_agents_without_custom_avatar.return_value = [{"agent_name": "../avatars2/x"}]
    monkeypatch.setattr(
        "services.docker_service.list_all_agents_fast",
        lambda: [SimpleNamespace(name="../avatars2/x")],
    )
    body = _run(avatar.generate_default_avatars(current_user=_admin()))
    assert body["generated"] == 0 and body["failed"] == 1, body
    assert (store.parent / "avatars2" / "x.webp").read_bytes() == SECRET
    assert list(store.iterdir()) == []


def test_bundled_avatar_install_writes_nothing_for_an_escaping_name(store, monkeypatch, tmp_path):
    tpl = tmp_path / "tpls" / "avtpl"
    tpl.mkdir(parents=True)
    (tpl / "avatar.webp").write_bytes(b"would-be-installed")
    monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (tmp_path / "tpls", tmp_path / "tpls"))
    crud._install_bundled_avatar(AgentConfig(name="../avatars2/x", template="local:avtpl"))
    assert (store.parent / "avatars2" / "x.webp").read_bytes() == SECRET
    assert list(store.iterdir()) == []


# =============================================================================
# 13. The three staging checks always agree, so a refusal comes before any write
# =============================================================================
# `_stage_config_files` checks its three staging names before the first `mkdir`.
# That order is only observable if the three verdicts can disagree. They cannot
# while every suffix is separator-free: the three paths then share every
# directory component. These tests read the suffixes from the real code (a spy
# on `_staging_path`), so they fail the day a suffix gains a separator, and pin
# that a refused name leaves the staging root exactly as it was.

_STAGING_NAME_PARTS = ["a", "q", ".", "/", "-", "\x00", "..", "/..", "../", "tmp", "_", "\u00e9", "agent-"]
_staging_names = st.lists(st.sampled_from(_STAGING_NAME_PARTS), max_size=10).map("".join)


@pytest.fixture
def staging_suffixes(staging, monkeypatch):
    """The suffixes `_stage_config_files` appends to `agent-<name>`, as the code
    spells them today."""
    seen = []
    real = crud._staging_path

    def spy(filename):
        seen.append(filename)
        return real(filename)

    monkeypatch.setattr(crud, "_staging_path", spy)
    try:
        crud._stage_config_files(AgentConfig(name="probe-agent"), {}, None)
    except OSError:
        # The names are recorded before any write; a suffix naming a directory
        # that does not exist must still reach the agreement check below.
        pass
    monkeypatch.setattr(crud, "_staging_path", real)
    prefix = "agent-probe-agent"
    assert len(seen) == 3 and all(f.startswith(prefix) for f in seen), seen
    return [f[len(prefix):] for f in seen]


def _staging_verdict(filename):
    try:
        crud._staging_path(filename)
    except HTTPException:
        return False
    return True


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(name="q/../..")
@given(name=_staging_names)
def test_the_three_staging_verdicts_always_agree(staging, staging_suffixes, monkeypatch, name):
    for root in (str(staging), f"{staging}/", "/" + str(staging), f"{staging}/./"):
        monkeypatch.setattr(crud, "_STAGING_ROOT", root)
        verdicts = [_staging_verdict(f"agent-{name}{s}") for s in staging_suffixes]
        assert len(set(verdicts)) == 1, (root, name, staging_suffixes, verdicts)


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(name="q/../..")
@given(name=_staging_names)
def test_a_refused_staging_name_creates_nothing(staging, tmp_path, name):
    before = set(tmp_path.rglob("*"))
    try:
        crud._stage_config_files(AgentConfig(name=name), {}, None)
    except HTTPException as exc:
        assert exc.detail["code"] == "INVALID_AGENT_NAME"
        assert set(tmp_path.rglob("*")) == before, name
    except OSError:
        # Not a refusal: a name like "/" names a missing parent directory.
        pass
    finally:
        # Undo an accepted name's files so every example starts from the same tree.
        for path in sorted(set(tmp_path.rglob("*")) - before, reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
