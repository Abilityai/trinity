"""trinity-enterprise#1066 — `GET /api/credentials/read` stays inside the agent home.

The agent image's read route takes a comma-separated list of paths. Its guard
was a string dance (`lstrip("/").lstrip(".")`, a `".." in` test, and a
`str(resolved).startswith(str(home))` prefix compare) with two holes:

* a single leading `../` survives it — `lstrip(".")` eats the dots, so the
  `..` test never sees them, and the unstripped path is joined to the home;
* the prefix compare has no separator, so `/…/home2` "starts with" `/…/home`.

Together a request could READ a file in a sibling directory whose name
extends the home's, and — since ent#1066 added an `lstat` to tell "absent"
from "unreadable" — learn whether any path one level up exists, through the
new `missing` list.

Every case below drives the real router (mounted over a tmp home) and asserts
on the response: an escaping path is neither read nor reported `missing`.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.unit

SECRET = "OUTSIDE=do-not-read-me\n"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from agent_server.routers import credentials as cred_router

    home = tmp_path / "home"
    home.mkdir()
    # A sibling whose name EXTENDS the home's — the prefix-compare shape.
    sibling = tmp_path / "home2"
    sibling.mkdir()
    (sibling / ".env").write_text(SECRET)
    (tmp_path / "outside.env").write_text(SECRET)
    monkeypatch.setattr(cred_router, "_HOME", home)
    app = FastAPI()
    app.include_router(cred_router.router)

    def read(paths: str) -> dict:
        async def go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent"
            ) as c:
                r = await c.get("/api/credentials/read", params={"paths": paths})
                assert r.status_code == 200, r.text
                return r.json()

        import asyncio
        return asyncio.run(go())

    agent = type("Agent", (), {})()
    agent.home, agent.root, agent.read = home, tmp_path, read
    return agent


def _nothing_about(body: dict, rel: str) -> None:
    assert rel not in body["files"], body
    assert rel not in body.get("files_b64", {}), body
    assert rel not in body["missing"], body
    assert SECRET not in str(body)


class TestEscapesAreRefused:

    def test_a_leading_dot_dot_into_a_sibling_is_not_read(self, agent):
        """`../home2/.env`: `lstrip(".")` hid the `..`, and `/…/home2` passed
        the separator-less prefix compare."""
        _nothing_about(agent.read("../home2/.env"), "../home2/.env")

    def test_a_leading_dot_dot_is_not_an_existence_oracle(self, agent):
        """An absent path outside the home must not come back `missing` —
        that list is a proof of absence, and outside the home it is a probe."""
        _nothing_about(agent.read("../no-such-file"), "../no-such-file")
        _nothing_about(agent.read("../outside.env"), "../outside.env")

    def test_a_deeper_dot_dot_is_refused(self, agent):
        for rel in ("../../etc/passwd", "a/../../outside.env", "./../outside.env"):
            _nothing_about(agent.read(rel), rel)

    def test_an_absolute_path_is_refused(self, agent):
        """Absolute paths are not relative to the home; they used to be
        silently re-rooted under it and could come back `missing`."""
        outside = str(agent.root / "outside.env")
        _nothing_about(agent.read(outside), outside)
        _nothing_about(agent.read("/etc/passwd"), "/etc/passwd")

    def test_a_link_to_a_sibling_file_is_not_followed(self, agent):
        os.symlink(agent.root / "home2" / ".env", agent.home / ".env")
        _nothing_about(agent.read(".env"), ".env")

    def test_a_link_out_of_the_home_is_not_followed(self, agent):
        os.symlink(agent.root / "outside.env", agent.home / ".env")
        _nothing_about(agent.read(".env"), ".env")

    def test_a_dangling_link_out_is_neither_read_nor_missing(self, agent):
        os.symlink(agent.root / "absent-target", agent.home / ".env")
        _nothing_about(agent.read(".env"), ".env")

    def test_a_linked_parent_directory_out_is_not_followed(self, agent):
        os.symlink(agent.root / "home2", agent.home / "cfg")
        _nothing_about(agent.read("cfg/.env"), "cfg/.env")


class TestTheHomeStillReads:
    """The containment must not cost a legitimate read."""

    def test_a_dotfile_is_read(self, agent):
        (agent.home / ".env").write_text("A=1\n")
        assert agent.read(".env")["files"] == {".env": "A=1\n"}

    def test_a_nested_file_is_read(self, agent):
        (agent.home / ".config").mkdir()
        (agent.home / ".config" / "c.json").write_text("{}")
        assert agent.read(".config/c.json")["files"] == {".config/c.json": "{}"}

    def test_a_link_inside_the_home_is_read(self, agent):
        (agent.home / "real.env").write_text("B=2\n")
        os.symlink(agent.home / "real.env", agent.home / ".env")
        assert agent.read(".env")["files"] == {".env": "B=2\n"}

    def test_an_absent_file_in_the_home_is_missing(self, agent):
        body = agent.read(".env,.mcp.json")
        assert body["missing"] == [".env", ".mcp.json"] and body["files"] == {}

    def test_any_dot_dot_component_is_still_refused(self, agent):
        """Unchanged from before: a `..` anywhere is refused, even one that
        would land back inside — no caller sends one."""
        (agent.home / "a").mkdir()
        (agent.home / ".env").write_text("C=3\n")
        _nothing_about(agent.read("a/../.env"), "a/../.env")
