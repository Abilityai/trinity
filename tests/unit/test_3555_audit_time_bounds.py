"""#3555: inclusive audit time filters compare instants, not ISO spellings.

The real SQLAlchemy table and operations run against a temporary SQLite file.
The router tests mount only the audit router, with real require_admin and DB
operations, so they need no running stack, account, Redis or Docker.
"""

import csv
import importlib.util
import io
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit
_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_TIMESTAMPS = [
    "2026-10-11T10:30:51.999999Z",
    "2026-10-11T10:30:52Z",
    "2026-10-11T10:30:52.000000Z",
    "2026-10-11T10:30:52.0Z",
    "2026-10-11T10:30:52.000001Z",
    "2026-10-11T10:30:52.100000Z",
    "2026-10-11T10:30:52.1Z",
    "2026-10-11T10:30:52.741692Z",
    "2026-10-11T10:30:52.999999Z",
    "2026-10-11T10:30:53Z",
]
_WINDOWS = [
    ("2026-10-11T10:30:52Z", None),
    (None, "2026-10-11T10:30:52Z"),
    ("2026-10-11T10:30:52Z", "2026-10-11T10:30:52Z"),
    ("2026-10-11T10:30:52.000000Z", "2026-10-11T10:30:52.000000Z"),
    ("2026-10-11T10:30:52.0Z", "2026-10-11T10:30:52.0Z"),
    ("2026-10-11T10:30:52.1Z", "2026-10-11T10:30:52.1Z"),
    ("2026-10-11T10:30:52.741692Z", "2026-10-11T10:30:52.741692Z"),
    ("2026-10-11T10:30:52.000001Z", "2026-10-11T10:30:52.999999Z"),
    ("2026-10-11T10:30:52+00:00", "2026-10-11T10:30:52.741692+00:00"),
    ("2026-10-11T11:30:52+01:00", "2026-10-11T05:30:52.741692-05:00"),
    ("2026-10-11T10:30:53Z", "2026-10-11T10:30:52Z"),
    (None, None),
]


def _load(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audit.db'}")
    package = types.ModuleType("_audit3555_db")
    package.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    engine_module = _load(monkeypatch, "_audit3555_db.engine", _BACKEND / "db/engine.py")
    tables = _load(monkeypatch, "_audit3555_db.tables", _BACKEND / "db/tables.py")
    module = _load(monkeypatch, "_audit3555_db.audit", _BACKEND / "db/audit.py")
    engine = engine_module.get_engine()
    tables.audit_log.create(engine)
    operations = module.PlatformAuditOperations()
    for index, timestamp in enumerate(_TIMESTAMPS):
        operations.create_audit_entry({
            "event_id": f"event-{index}",
            "event_type": "mcp_operation",
            "event_action": "call",
            "actor_type": "user",
            "actor_id": "operator",
            "target_type": "agent",
            "target_id": "worker",
            "source": "mcp",
            "timestamp": timestamp,
            "mcp_key_id": "key-a",
            "mcp_scope": "user",
            "request_id": "request-a",
            "details": '{"result":"ok"}',
            "previous_hash": f"previous-{index}",
            "entry_hash": f"hash-{index}",
        })
    try:
        yield operations, engine, tables.audit_log
    finally:
        engine.dispose()


def _expected(start, end):
    lower = datetime.fromisoformat(start) if start else None
    upper = datetime.fromisoformat(end) if end else None
    return {
        f"event-{index}"
        for index, timestamp in enumerate(_TIMESTAMPS)
        if (lower is None or datetime.fromisoformat(timestamp) >= lower)
        and (upper is None or datetime.fromisoformat(timestamp) <= upper)
    }


@pytest.mark.parametrize("start,end", _WINDOWS)
@pytest.mark.parametrize("reader", ["entries", "count", "stats", "calendar", "heatmap"])
def test_3555_all_readers_use_inclusive_instants(audit_db, start, end, reader):
    operations, _, _ = audit_db
    filters = {"start_time": start, "end_time": end}
    expected = _expected(start, end)
    if reader == "entries":
        rows = operations.get_audit_entries(**filters)
        assert {row["event_id"] for row in rows} == expected
        # A read must not canonicalize the immutable payload returned to users.
        for row in rows:
            index = int(row["event_id"].split("-")[1])
            assert row["timestamp"] == _TIMESTAMPS[index]
            assert row["entry_hash"] == f"hash-{index}"
            assert row["previous_hash"] == f"previous-{index}"
    elif reader == "count":
        assert operations.count_audit_entries(**filters) == len(expected)
    else:
        result = getattr(operations, f"get_audit_{reader}")(**filters)
        assert result["total"] == len(expected)
        if reader == "stats":
            counts = {"mcp_operation": len(expected)} if expected else {}
            assert result["by_event_type"] == counts
            actors = {"user": len(expected)} if expected else {}
            assert result["by_actor_type"] == actors
        else:
            buckets = result["days"] if reader == "calendar" else result["cells"]
            assert sum(bucket["count"] for bucket in buckets) == len(expected)


def test_3555_other_filters_and_pagination_are_preserved(audit_db):
    operations, _, _ = audit_db
    filters = {
        "start_time": "2026-10-11T10:30:52Z",
        "end_time": "2026-10-11T10:30:52.999999Z",
        "event_type": "mcp_operation",
        "actor_type": "user",
        "actor_id": "operator",
        "target_type": "agent",
        "target_id": "worker",
        "source": "mcp",
        "request_id": "request-a",
        "mcp_key_id": "key-a",
        "mcp_scope": "user",
    }
    for field in filters.keys() - {"start_time", "end_time"}:
        entry = operations.get_audit_entry("event-7")
        entry["event_id"] = f"excluded-{field}"
        entry[field] = "other"
        operations.create_audit_entry(entry)
    expected = _expected(filters["start_time"], filters["end_time"])
    rows = operations.get_audit_entries(**filters)
    assert {row["event_id"] for row in rows} == expected
    assert operations.count_audit_entries(**filters) == len(expected)
    ordered = sorted(rows, key=lambda row: (row["timestamp"], row["id"]), reverse=True)
    assert rows == ordered
    assert operations.get_audit_entries(**filters, limit=2, offset=1) == ordered[1:3]
    assert operations.get_audit_entries(**filters, limit=2, offset=len(expected)) == []
    for reader in (operations.get_audit_heatmap, operations.get_audit_calendar):
        result = reader(
            start_time=filters["start_time"], end_time=filters["end_time"],
            event_type="mcp_operation", actor_type="user",
        )
        # These readers expose only event_type and actor_type, not the other filters.
        assert result["total"] == len(expected) + len(filters) - 4


@pytest.fixture
def audit_client(audit_db, monkeypatch):
    from models import User

    operations, _, _ = audit_db
    database = types.ModuleType("database")
    database.db = operations
    monkeypatch.setitem(sys.modules, "database", database)
    dependencies = _load(monkeypatch, "_audit3555_dependencies", _BACKEND / "dependencies.py")
    monkeypatch.setitem(sys.modules, "dependencies", dependencies)
    service = types.ModuleType("services.platform_audit_service")
    service.platform_audit_service = object()
    monkeypatch.setitem(sys.modules, "services.platform_audit_service", service)
    router = _load(monkeypatch, "_audit3555_router", _BACKEND / "routers/audit_log.py")
    app = FastAPI()
    app.include_router(router.router)
    principal = {"role": "admin"}

    def current_user():
        return User(id=1, username="operator", role=principal["role"])

    app.dependency_overrides[dependencies.get_current_user] = current_user
    with TestClient(app) as client:
        yield client, principal


@pytest.mark.parametrize("format", ["json", "csv"])
def test_3555_export_uses_real_time_filtered_rows(audit_client, format):
    client, _ = audit_client
    start = "2026-10-11T10:30:52Z"
    end = "2026-10-11T10:30:52.741692Z"
    response = client.get("/api/audit-log/export", params={
        "start_time": start, "end_time": end, "format": format,
    })
    assert response.status_code == 200
    if format == "json":
        data = response.json()
        rows = data["entries"]
        assert data["count"] == len(_expected(start, end))
    else:
        assert response.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {row["event_id"] for row in rows} == _expected(start, end)
    for row in rows:
        index = int(row["event_id"].split("-")[1])
        assert row["timestamp"] == _TIMESTAMPS[index]
        assert row["entry_hash"] == f"hash-{index}"


@pytest.mark.parametrize("path", ["", "/stats", "/calendar", "/heatmap", "/export"])
def test_3555_read_routes_still_reject_non_admin(audit_client, path):
    client, principal = audit_client
    principal["role"] = "user"
    response = client.get(f"/api/audit-log{path}", params={
        "start_time": "2026-10-11T10:30:52Z", "end_time": "2026-10-11T10:30:53Z",
    })
    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


def test_3555_export_still_requires_bounds_and_valid_format(audit_client):
    client, _ = audit_client
    assert client.get("/api/audit-log/export").status_code == 422
    response = client.get("/api/audit-log/export", params={
        "start_time": "2026-10-11T10:30:52Z", "end_time": "2026-10-11T10:30:53Z",
        "format": "xml",
    })
    assert response.status_code == 400
