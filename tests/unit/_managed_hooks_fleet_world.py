"""A fleet for the managed-hooks check tests (trinity-enterprise#1176, #4778).

The check's reads of the fleet (Docker, each agent's `/health`, the ephemeral
flag) are stubbed. Its alert is REAL: `platform_alerts.observe` / `clear` run
against the unit island's SQLite through spies, on a queue name of this test's
own, so a test sees the row the check itself wrote and no other test's. The
check decides from that row, so a fake row would prove only the fake.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import managed_hooks_check_service as mhc  # noqa: E402
from services import operator_resume_service  # noqa: E402
from services import platform_alerts  # noqa: E402

# Captured before any test patches it: the queue the alert really lives on.
REAL_ALERT_AGENT_NAME = mhc.ALERT_AGENT_NAME


class Fleet:
    def __init__(self):
        self.states = {}          # name -> "running" | "stopped"; None = Docker unreadable
        self.health = {}          # name -> /health value, or None = no answer
        self.ephemeral = set()
        self.queue = f"sys-mhc-{uuid.uuid4().hex[:8]}"
        self._observe = platform_alerts.observe
        self.observe = MagicMock(wraps=platform_alerts.observe)
        self.clear = MagicMock(wraps=platform_alerts.clear)

    def agent(self, name, value="ok", *, state="running", ephemeral=False):
        self.states[name] = state
        self.health[name] = value
        if ephemeral:
            self.ephemeral.add(name)
        return self

    def install(self, monkeypatch):
        async def states():
            return self.states

        async def read(name):
            return self.health[name]
        monkeypatch.setattr(mhc, "_container_states", states)
        monkeypatch.setattr(mhc, "_read_registration", read)
        monkeypatch.setattr(mhc, "_is_ephemeral", lambda name: name in self.ephemeral)
        monkeypatch.setattr(mhc, "ALERT_AGENT_NAME", self.queue)
        monkeypatch.setattr(platform_alerts, "observe", self.observe)
        monkeypatch.setattr(platform_alerts, "clear", self.clear)
        # A row's follow-up (a websocket trigger) must not outlive the test's loop.
        monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
        return self

    def run(self):
        return asyncio.run(mhc.run_once())

    # -- the alert's row -----------------------------------------------------

    def file_alert(self, context):
        """A pending alert this process did not write in this test: another
        worker's pass, or a pass from before a restart."""
        agents = context.get("agents") if isinstance(context, dict) else None
        text = mhc.describe([a for a in agents if isinstance(a, str)]
                            if isinstance(agents, list) else [])
        return self._observe(self.queue, mhc.ALERT_KIND, mhc.ALERT_KEY, priority="high",
                             title=text["title"], question=text["question"], context=context)

    def row(self):
        from database import db
        return db.find_pending_operator_queue_by_subject(
            self.queue, platform_alerts.subject_for(mhc.ALERT_KIND, mhc.ALERT_KEY))

    def named(self):
        row = self.row()
        return None if row is None else row["context"]["agents"]

    def person_ends_the_alert(self):
        from database import db
        db.respond_to_operator_queue_item(self.row()["id"], "ack", "seen", None, "person@example.com")
