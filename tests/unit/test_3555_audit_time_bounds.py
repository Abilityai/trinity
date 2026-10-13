"""#3555: inclusive audit time filters compare instants, not ISO spellings.

The real SQLAlchemy table and operations run against a temporary SQLite file.
The router tests mount only the audit router, with real require_admin and DB
operations, so they need no running stack, account, Redis or Docker.
"""

import csv
import importlib.util
import io
import json
import sys
import types
from datetime import datetime, timedelta
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
    if end and "." not in end and "," not in end:
        # Without a fraction the upper bound covers that whole second.
        upper += timedelta(microseconds=999999)
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
        entry["details"] = json.dumps(entry["details"])
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


def test_3555_list_route_keeps_count_and_page_consistent(audit_client):
    client, _ = audit_client
    response = client.get("/api/audit-log", params={
        "start_time": "2026-10-11T10:30:52Z",
        "end_time": "2026-10-11T10:30:52.000000Z",
        "event_type": "mcp_operation", "source": "mcp",
        "mcp_key_id": "key-a", "mcp_scope": "user", "limit": 2, "offset": 1,
    })
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["entries"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 1
    assert {row["event_id"] for row in data["entries"]} == {"event-3", "event-2"}


@pytest.mark.parametrize("filters", [
    {"start_time": "not-a-timestamp"}, {"end_time": "not-a-timestamp"},
    {"start_time": "", "end_time": ""},
])
def test_3555_invalid_bounds_retain_legacy_string_behavior(audit_db, filters):
    operations, _, _ = audit_db
    expected = {
        f"event-{index}" for index, timestamp in enumerate(_TIMESTAMPS)
        if (not filters.get("start_time") or timestamp >= filters["start_time"])
        and (not filters.get("end_time") or timestamp <= filters["end_time"])
    }
    assert {row["event_id"] for row in operations.get_audit_entries(**filters)} == expected
    assert operations.count_audit_entries(**filters) == len(expected)


def test_3555_export_still_requires_bounds_and_valid_format(audit_client):
    client, _ = audit_client
    assert client.get("/api/audit-log/export").status_code == 422
    response = client.get("/api/audit-log/export", params={
        "start_time": "2026-10-11T10:30:52Z", "end_time": "2026-10-11T10:30:53Z",
        "format": "xml",
    })
    assert response.status_code == 400


_DAY = [
    ("2026-05-17T23:59:59.999999Z", False),
    ("2026-05-18T00:00:00Z", True),
    ("2026-05-18T12:00:00Z", True),
    ("2026-05-18T23:59:59.0Z", True),
    ("2026-05-18T23:59:59.4Z", True),
    ("2026-05-18T23:59:59.999999Z", True),
    ("2026-05-18T23:59:59Z", True),
    ("2026-05-19T00:00:00Z", False),
    ("2026-05-18T23:30:00Z", True),
    ("2026-05-18T23:59:30Z", True),
]


def _add_day_rows(operations):
    """Rows around the 2026-05-18 UTC day, as the calendar drill-down queries it."""
    for index, (timestamp, _) in enumerate(_DAY):
        entry = operations.get_audit_entry("event-0")
        entry["event_id"] = f"day-{index}"
        entry["timestamp"] = timestamp
        entry["details"] = json.dumps(entry["details"])
        operations.create_audit_entry(entry)
    return {f"day-{index}" for index, (_, inside) in enumerate(_DAY) if inside}


_DRILLDOWN = {"start_time": "2026-05-18T00:00:00Z", "end_time": "2026-05-18T23:59:59Z"}


def test_3555_day_drilldown_keeps_the_last_second_of_the_day(audit_db):
    operations, _, _ = audit_db
    inside = _add_day_rows(operations)
    assert {row["event_id"] for row in operations.get_audit_entries(**_DRILLDOWN)} == inside
    assert operations.count_audit_entries(**_DRILLDOWN) == len(inside)
    for reader in (operations.get_audit_stats, operations.get_audit_calendar,
                   operations.get_audit_heatmap):
        assert reader(**_DRILLDOWN)["total"] == len(inside)
    days = operations.get_audit_calendar(**_DRILLDOWN)["days"]
    assert [(day["date"], day["count"]) for day in days] == [("2026-05-18", len(inside))]


_WHOLE_DAY = {"day-1", "day-2", "day-3", "day-4", "day-5", "day-6", "day-8", "day-9"}
# An upper bound without a fraction covers the end of the unit it states.
_COARSE_ENDS = [
    ("2026-05-18T23:59:59Z", _WHOLE_DAY),
    ("2026-05-18T23:59Z", _WHOLE_DAY),
    ("2026-05-18T23Z", _WHOLE_DAY),
    ("2026-05-18T23:59", _WHOLE_DAY),
    ("2026-05-18T23", _WHOLE_DAY),
    ("20260518T2359Z", _WHOLE_DAY),
    ("2026-05-19T05:59+06:00", _WHOLE_DAY),
    ("2026-05-18T23:58Z", {"day-1", "day-2", "day-8"}),
    ("2026-05-18T22Z", {"day-1", "day-2"}),
    # Exact instants: date-only (midnight) and any bound with a fraction.
    ("2026-05-18", {"day-1"}),
    ("2026-05-18T23:59:00.0Z", {"day-1", "day-2", "day-8"}),
    ("2026-05-18T23:30:00.000000Z", {"day-1", "day-2", "day-8"}),
]


def test_3555_day_drilldown_route_keeps_the_last_second_of_the_day(audit_client, audit_db):
    client, _ = audit_client
    inside = _add_day_rows(audit_db[0])
    assert inside == _WHOLE_DAY
    data = client.get("/api/audit-log", params={**_DRILLDOWN, "limit": 100}).json()
    assert data["total"] == len(inside)
    assert {row["event_id"] for row in data["entries"]} == inside
    exported = client.get("/api/audit-log/export", params=_DRILLDOWN).json()
    assert {row["event_id"] for row in exported["entries"]} == inside


@pytest.mark.parametrize("end,expected", _COARSE_ENDS)
def test_3555_upper_bound_covers_the_unit_it_states(audit_client, audit_db, end, expected):
    client, _ = audit_client
    _add_day_rows(audit_db[0])
    params = {"start_time": _DRILLDOWN["start_time"], "end_time": end}
    data = client.get("/api/audit-log", params={**params, "limit": 100}).json()
    assert {row["event_id"] for row in data["entries"]} == expected
    assert data["total"] == len(expected)
    exported = client.get("/api/audit-log/export", params=params).json()
    assert {row["event_id"] for row in exported["entries"]} == expected
    for reader in ("stats", "calendar", "heatmap"):
        assert client.get(f"/api/audit-log/{reader}", params=params).json()["total"] == len(expected)


def test_3555_lower_bounds_stay_exact(audit_db):
    operations, _, _ = audit_db
    _add_day_rows(operations)
    # A lower bound is never widened: 23:59Z starts at 23:59:00, not at 23:59:59.999999.
    ids = {row["event_id"] for row in operations.get_audit_entries(
        start_time="2026-05-18T23:59Z", end_time="2026-05-18T23:59:59Z")}
    assert ids == {"day-3", "day-4", "day-5", "day-6", "day-9"}
    ids = {row["event_id"] for row in operations.get_audit_entries(start_time="2026-05-18")}
    assert ids >= {"day-1", "day-2", "day-7"} and "day-0" not in ids


@pytest.mark.parametrize("start,end,expected", [
    ("0999-01-01T00:00:00Z", None, "all"),
    ("0001-01-01T00:00:00Z", None, "all"),
    (None, "0999-12-31T23:59:59Z", {"old-0999"}),
    (None, "0999-12-31T23:59Z", {"old-0999"}),
    ("0999-06-15T12:00:00Z", "0999-06-15T12:00:00Z", {"old-0999"}),
    ("0999-06-15T12:00:01Z", None, "all-but-old"),
    (None, "0300-01-01T00:00:00Z", set()),
])
def test_3555_years_before_1000_compare_as_zero_padded_instants(audit_db, start, end, expected):
    # glibc strftime("%Y") does not zero-pad years below 1000 on Linux.
    operations, _, _ = audit_db
    entry = operations.get_audit_entry("event-0")
    entry["event_id"] = "old-0999"
    entry["timestamp"] = "0999-06-15T12:00:00Z"
    entry["details"] = json.dumps(entry["details"])
    operations.create_audit_entry(entry)
    everything = {f"event-{index}" for index in range(len(_TIMESTAMPS))} | {"old-0999"}
    if expected == "all":
        expected = everything
    elif expected == "all-but-old":
        expected = everything - {"old-0999"}
    ids = {row["event_id"] for row in operations.get_audit_entries(
        start_time=start, end_time=end, limit=100)}
    assert ids == expected


@pytest.mark.parametrize("bound,upper,text", [
    ("0999-01-01T00:00:00Z", False, "0999-01-01T00:00:00.000000"),
    ("0999-12-31T23:59:59Z", True, "0999-12-31T23:59:59.999999"),
    ("0999-12-31T23:59Z", True, "0999-12-31T23:59:59.999999"),
    ("0999-12-31T23Z", True, "0999-12-31T23:59:59.999999"),
    ("0999-12-31", True, "0999-12-31T00:00:00.000000"),
    ("9999-12-31T23:59:59Z", True, "9999-12-31T23:59:59.999999"),
])
def test_3555_bound_text_has_a_fixed_layout(audit_db, bound, upper, text):
    operations, _, _ = audit_db
    module = sys.modules["_audit3555_db.audit"]
    assert module._bound_to_utc_text(bound, not upper) == text


_UNTIL_59_4 = {"day-1", "day-2", "day-3", "day-4", "day-6", "day-8", "day-9"}
_UNTIL_59_0 = {"day-1", "day-2", "day-3", "day-6", "day-8", "day-9"}


@pytest.mark.parametrize("end,expected", [
    ("2026-05-18T23:59:59.4Z", _UNTIL_59_4),
    ("2026-05-18T23:59:59.400000Z", _UNTIL_59_4),
    ("2026-05-19T05:59:59.4+06:00", _UNTIL_59_4),
    ("2026-05-18T23:59:59,4Z", _UNTIL_59_4),
    ("2026-05-18T23:59:59.0Z", _UNTIL_59_0),
    ("2026-05-18T23:59:59.000000+00:00", _UNTIL_59_0),
])
def test_3555_upper_bound_with_a_fraction_stays_exact(audit_db, end, expected):
    operations, _, _ = audit_db
    _add_day_rows(operations)
    ids = {row["event_id"] for row in operations.get_audit_entries(
        start_time=_DRILLDOWN["start_time"], end_time=end)}
    # The .999999 row (day-5) is past a bound that names a fraction below it.
    assert ids == expected


def test_3555_upper_bound_without_a_fraction_covers_its_second_in_any_offset(audit_db):
    operations, _, _ = audit_db
    inside = _add_day_rows(operations)
    for end in ("2026-05-18T23:59:59+00:00", "2026-05-19T05:59:59+06:00",
                "2026-05-18T18:59:59-05:00", "2026-05-18T23:59:59"):
        ids = {row["event_id"] for row in operations.get_audit_entries(
            start_time=_DRILLDOWN["start_time"], end_time=end)}
        assert ids == inside, end


@pytest.mark.parametrize("filters,total", [
    ({"start_time": "0001-01-01T00:00:00+01:00"}, len(_TIMESTAMPS)),
    ({"end_time": "9999-12-31T23:59:59-01:00"}, len(_TIMESTAMPS)),
    ({"end_time": "0001-01-01T00:00:00+01:00"}, 0),
    ({"start_time": "9999-12-31T23:59:59-01:00"}, 0),
])
@pytest.mark.parametrize("path", ["", "/stats", "/calendar", "/heatmap", "/export"])
def test_3555_offset_bound_past_the_date_range_is_not_a_server_error(
    audit_client, filters, total, path,
):
    client, _ = audit_client
    params = {}
    if path == "/export":
        params = {"start_time": "2000-01-01T00:00:00Z", "end_time": "2100-01-01T00:00:00Z"}
    params.update(filters)
    response = client.get(f"/api/audit-log{path}", params=params)
    # The bound cannot be converted to UTC, so it keeps the legacy raw-string
    # comparison like any other bound that does not parse: no 500.
    assert response.status_code == 200
    body = response.json()
    assert body["count" if path == "/export" else "total"] == total
