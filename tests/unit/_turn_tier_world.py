"""A spec'd stand-in for the database, for the turn-tier tests of #4281.

The ent#926 tests use a bare `MagicMock`: `can_user_share_agent` is a canned
answer unrelated to the user rows, and any method nobody configured returns a
truthy mock. A refactor that reads the tier through another lookup could then
change a rating and stay green, and "the agent's owner has no user row" cannot
be stated consistently.

Here every answer is derived from ONE table of users and ONE ownership map, with
the real methods' semantics:

* `get_agent_owner` is the INNER JOIN of `agent_ownership` and `users`
  (`db/agents.py`): no answer when the owner's user row is missing;
* `can_user_share_agent` is `db/agent_settings/sharing.py`'s rule: an admin, or
  the username `get_agent_owner` names;
* `get_schedule` leaves out a soft-deleted row (`db/schedules/crud.py`), and the
  schedule id `BOOM` raises, a failed read;
* any other method raises `AttributeError`, so an unplanned lookup fails loudly.

Each method call is counted in `calls`, and `fail_after` makes a method raise
after its first N calls (a lookup that breaks mid-request).
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Dict, Optional

AGENT = "agent-a"          # owned by OWNER_ID
OTHER_AGENT = "agent-b"    # owned by OWNER_ID too; its schedules are not agent-a's
ORPHAN_AGENT = "agent-c"   # its ownership row names a user id with no user row

ADMIN_ID, OWNER_ID, MATE_ID, FORMER_OWNER_ID, DANGLING_ID = 1, 2, 3, 99, 404

# id -> (username, role, display name, email)
USERS = {
    ADMIN_ID: ("ada", "admin", "Ada Admin", "ada@example.com"),
    OWNER_ID: ("olga", "user", "", "olga@example.com"),
    MATE_ID: ("mate@example.com", "operator", "Mate Shared", "mate@example.com"),
}

OWNERSHIP = {AGENT: OWNER_ID, OTHER_AGENT: OWNER_ID, ORPHAN_AGENT: FORMER_OWNER_ID}

BOOM = "s-boom"

# schedule id -> (agent, creator id, soft-deleted)
SCHEDULES = {
    "s-owner": (AGENT, OWNER_ID, False),
    "s-admin": (AGENT, ADMIN_ID, False),
    "s-mate": (AGENT, MATE_ID, False),
    "s-dangling": (AGENT, DANGLING_ID, False),
    "s-none": (AGENT, None, False),
    "s-deleted": (AGENT, OWNER_ID, True),
    "s-other-agent": (OTHER_AGENT, OWNER_ID, False),
    "c-admin": (ORPHAN_AGENT, ADMIN_ID, False),
    "c-former-owner": (ORPHAN_AGENT, FORMER_OWNER_ID, False),
    "c-mate": (ORPHAN_AGENT, MATE_ID, False),
}


def _user(uid: int) -> Optional[Dict]:
    row = USERS.get(uid)
    if row is None:
        return None
    username, role, name, email = row
    return {"id": uid, "username": username, "role": role, "name": name, "email": email}


def schedule(sid: str) -> SimpleNamespace:
    agent, creator, deleted = SCHEDULES[sid]
    return SimpleNamespace(id=sid, name=f"brief {sid}", agent_name=agent, owner_id=creator,
                           deleted=deleted)


class World:
    """The database, as far as the turn tier reads it."""

    def __init__(self, *, executions=None, visitor_shell: bool = True, fail_after=None):
        self._executions = dict(executions or {})
        self._visitor_shell = visitor_shell
        self._fail_after = dict(fail_after or {})
        self.calls: Counter = Counter()

    def _count(self, method: str) -> None:
        self.calls[method] += 1
        limit = self._fail_after.get(method)
        if limit is not None and self.calls[method] > limit:
            raise RuntimeError(f"{method} failed")

    # -- users -----------------------------------------------------------------
    def get_user_by_id(self, user_id):
        self._count("get_user_by_id")
        return _user(user_id) if isinstance(user_id, int) else None

    def get_user_by_username(self, username):
        self._count("get_user_by_username")
        for uid, row in USERS.items():
            if row[0] == username:
                return _user(uid)
        return None

    def get_user_by_email(self, email):
        self._count("get_user_by_email")
        for uid, row in USERS.items():
            if row[3] == email:
                return _user(uid)
        return None

    # -- ownership -------------------------------------------------------------
    def get_agent_owner(self, agent_name):
        self._count("get_agent_owner")
        owner_id = OWNERSHIP.get(agent_name)
        user = _user(owner_id) if owner_id is not None else None
        if user is None:  # the INNER JOIN on users
            return None
        return {"agent_name": agent_name, "owner_id": owner_id, "owner_username": user["username"],
                "is_system": False}

    def can_user_share_agent(self, username, agent_name):
        self._count("can_user_share_agent")
        user = self.get_user_by_username(username)
        if not user:
            return False
        if user["role"] == "admin":
            return True
        owner = self.get_agent_owner(agent_name)
        return bool(owner and owner["owner_username"] == username)

    # -- schedules, executions, settings ---------------------------------------
    def get_schedule(self, schedule_id):
        self._count("get_schedule")
        if schedule_id == BOOM:
            raise RuntimeError("schedule read failed")
        if schedule_id not in SCHEDULES:
            return None
        row = schedule(schedule_id)
        return None if row.deleted else row

    def get_execution(self, execution_id):
        self._count("get_execution")
        return self._executions.get(execution_id)

    def get_guardrails_config(self, agent_name):
        self._count("get_guardrails_config")
        return {"visitor_shell": self._visitor_shell}
