"""`triggered_by=room` must actually filter (ent#169).

`routers/executions.py` treats `_VALID_TRIGGERS` as an allow-list and degrades an
unrecognised value to `None` — i.e. NO filter — so an unlisted trigger silently
returns EVERY execution rather than none. Verified live before the fix:

    triggered_by=room          -> 14 rows, kinds=['agent','public','room']
    triggered_by=bogus-trigger -> 14 rows, kinds=['agent','public','room']

Identical to a garbage value: the filter lied. ent#169 introduces `room` as a
runtime `triggered_by`, so it has to be listed or the Sessions UI (ent#170)
cannot filter room executions and users get silently wrong data.
"""
from __future__ import annotations


def test_room_is_an_accepted_trigger_filter():
    from routers.executions import _VALID_TRIGGERS

    assert "room" in _VALID_TRIGGERS, (
        "an unlisted trigger degrades to NO filter, so `triggered_by=room` "
        "would return every execution instead of the room's"
    )


def test_every_known_trigger_is_filterable():
    """Any value written to `triggered_by` that a user can filter on must be
    listed; otherwise its filter silently returns everything."""
    from routers.executions import _VALID_TRIGGERS

    for trigger in ("schedule", "manual", "agent", "mcp", "chat", "session",
                    "public", "webhook", "fan_out", "loop", "reminder", "room"):
        assert trigger in _VALID_TRIGGERS
