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
* ``a..b`` stays a valid agent name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import routers.agent_rename as agent_rename
import routers.avatar as avatar
from models import AgentConfig, User
from services import template_service
from services.agent_service import crud
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


def _rename_env(monkeypatch, *, rename_side_effect=None):
    container = MagicMock(status="exited")
    db = MagicMock()
    db.can_user_rename_agent.return_value = True
    db.is_volume_base_reserved.return_value = False
    db.rename_agent.return_value = True
    monkeypatch.setattr(agent_rename, "db", db)
    monkeypatch.setattr(
        agent_rename, "get_agent_container",
        lambda name: None if name == "new-name" else container,
    )
    monkeypatch.setattr(agent_rename, "container_stop", AsyncMock())
    monkeypatch.setattr(agent_rename, "container_rename", AsyncMock(side_effect=rename_side_effect))
    monkeypatch.setattr(agent_rename.platform_audit_service, "log", AsyncMock())
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

