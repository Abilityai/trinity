"""#3584 — an expiry can be removed from a public link, and only on purpose.

`PUT /api/agents/{name}/public-links/{id}` read `expires_at=None` as "unchanged",
so the Edit dialog had no way to remove an expiry. Now an `expires_at` the
request SENDS as null or empty clears it; a request that leaves the field out
(a rename, the enable toggle) keeps it.

The store half runs on a real SQLite built by the platform's own `init_schema`;
the router half drives the real handler with the store and container stubbed.
"""
import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

EXPIRY = "2026-10-22T11:00:00.000Z"


@pytest.fixture()
def links(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3584.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from database import db
    return db


def _link(db):
    # `db.engine` is cached across tests, so every test uses its own agent name.
    return db.create_public_link(f"agent-{uuid.uuid4().hex[:8]}", "owner", name="Demo", expires_at=EXPIRY)


class TestStore:
    def test_clear_expiry_removes_it(self, links):
        link = _link(links)
        assert links.update_public_link(link["id"], clear_expiry=True)["expires_at"] is None

    def test_an_update_without_an_expiry_keeps_it(self, links):
        link = _link(links)
        after = links.update_public_link(link["id"], name="Renamed", enabled=False)
        assert after["expires_at"] == EXPIRY and after["name"] == "Renamed"

    def test_a_new_expiry_replaces_it(self, links):
        link = _link(links)
        new = "2026-10-30T08:00:00.000Z"
        assert links.update_public_link(link["id"], expires_at=new)["expires_at"] == new


class TestRouter:
    """The handler tells "sent as null" from "not sent" (`model_fields_set`)."""

    @pytest.fixture()
    def call(self, monkeypatch):
        import routers.public_links as pl
        from db_models import PublicLinkUpdate

        seen = {}

        def _update(**kw):
            seen.update(kw)
            return {"id": "l1", "agent_name": "bot"}

        monkeypatch.setattr(pl, "get_agent_container", lambda name: object())
        monkeypatch.setattr(pl, "manager", None, raising=False)
        monkeypatch.setattr(pl, "db", SimpleNamespace(
            get_public_link=lambda lid: {"id": lid, "agent_name": "bot"},
            update_public_link=_update,
        ))
        monkeypatch.setattr(pl, "_link_to_response", lambda row, **kw: row)
        request = SimpleNamespace(client=None, scope={"path": "/api/agents/bot/public-links/l1"})

        def run(body: dict):
            seen.clear()
            asyncio.run(pl.update_public_link("bot", "l1", PublicLinkUpdate(**body), request))
            return dict(seen)
        return run

    @pytest.mark.parametrize("sent", [None, ""])
    def test_an_expiry_sent_empty_clears_it(self, call, sent):
        kw = call({"expires_at": sent})
        assert kw["clear_expiry"] is True and kw["expires_at"] is None

    @pytest.mark.parametrize("body", [{}, {"name": "Renamed"}, {"enabled": False}])
    def test_an_expiry_left_out_is_kept(self, call, body):
        kw = call(body)
        assert kw["clear_expiry"] is False and kw["expires_at"] is None

    def test_an_expiry_sent_is_stored(self, call):
        kw = call({"expires_at": EXPIRY})
        assert kw["clear_expiry"] is False and kw["expires_at"] == EXPIRY
