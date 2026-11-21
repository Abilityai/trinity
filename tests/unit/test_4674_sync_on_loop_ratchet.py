"""Blocking calls inside `async def` may only shrink (#4674).

`scripts/ci/check_sync_on_loop.py` counts, per file, the synchronous db /
Docker / HTTP / sleep calls written directly inside an `async def` — each one
blocks the whole uvicorn worker while it runs. This suite is the ratchet
itself (the last test) plus the scanner's rules, because a scanner that
over- or under-counts makes the ratchet either noise or theatre.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CI = str(_REPO / "scripts" / "ci")
if _CI not in sys.path:
    sys.path.insert(0, _CI)

pytestmark = pytest.mark.unit

import check_sync_on_loop as guard  # noqa: E402


def kinds(source: str):
    return [(fn, kind) for _, fn, kind in guard.scan_source(textwrap.dedent(source))]


class TestWhatCounts:
    def test_a_db_call_in_an_async_def(self):
        assert kinds("""
            async def handler(name):
                user = db.get_user_by_username(name)
                return user
        """) == [("handler", "db")]

    @pytest.mark.parametrize("call", ["core_db.get_agent_owner(n)", "_db.get_setting_value('k')",
                                      "deps.db.get_setting_value('k')"])
    def test_every_spelling_of_the_db_layer(self, call):
        assert kinds(f"async def f(n):\n    return {call}\n") == [("f", "db")]

    @pytest.mark.parametrize("call, kind", [
        ("get_agent_container(n)", "docker"),
        ("list_all_agents_fast()", "docker"),
        ("docker_service.agent_container_states()", "docker"),
        ("time.sleep(1)", "time"),
        ("subprocess.run(['git', 'status'])", "subprocess"),
        ("requests.get('http://x')", "requests"),
    ])
    def test_docker_and_blocking_stdlib(self, call, kind):
        assert kinds(f"async def f(n):\n    return {call}\n") == [("f", kind)]

    def test_each_call_is_counted(self):
        assert len(kinds("""
            async def f(n):
                a = db.get_agent_owner(n)
                b = db.get_agent_owner(n)
                if a:
                    return [db.get_user_by_email(e) for e in b]
        """)) == 3

    def test_a_call_through_the_modules_own_sync_function(self):
        """The heartbeat-watch shape: the loop itself awaits nothing blocking,
        the sync function one line down lists every container."""
        assert kinds("""
            def process_tick():
                return list_all_agents_fast()

            def outer():
                return process_tick()

            async def watch_loop():
                while True:
                    outer()
                    await asyncio.sleep(5)
        """) == [("watch_loop", "via:docker")]


class TestWhatDoesNot:
    def test_a_plain_def_handler_runs_in_the_threadpool(self):
        assert kinds("def handler(name):\n    return db.get_user_by_username(name)\n") == []

    @pytest.mark.parametrize("hop", [
        "await asyncio.to_thread(db.get_user_by_username, name)",
        "await asyncio.to_thread(lambda: db.get_user_by_username(name))",
        "await loop.run_in_executor(None, db.get_user_by_username, name)",
        "await run_in_threadpool(db.get_user_by_username, name)",
        "await anyio.to_thread.run_sync(db.get_user_by_username, name)",
    ])
    def test_a_thread_hop(self, hop):
        assert kinds(f"async def f(name):\n    return {hop}\n") == []

    def test_a_nested_def_is_not_this_frame(self):
        assert kinds("""
            async def f(name):
                def read():
                    return db.get_user_by_username(name)
                return await asyncio.to_thread(read)
        """) == []

    def test_async_and_unrelated_calls(self):
        assert kinds("""
            async def f(name):
                await asyncio.sleep(1)
                row = await client.get(name)
                other.db_like(name)
                database.connect()
                return time.monotonic()
        """) == []

    def test_a_call_made_beside_a_hop_still_counts(self):
        """Only the hop's ARGUMENTS are off the loop."""
        assert kinds("""
            async def f(name):
                owner = db.get_agent_owner(name)
                return await asyncio.to_thread(work, owner)
        """) == [("f", "db")]


class TestCompare:
    def test_equal_holds(self):
        assert guard.compare({"a.py": 3}, {"a.py": 3}) == []

    def test_growth_fails(self):
        (problem,) = guard.compare({"a.py": 4}, {"a.py": 3})
        assert problem.startswith("GREW  a.py: 4") and "baseline 3" in problem

    def test_a_new_file_starts_at_zero(self):
        (problem,) = guard.compare({"a.py": 3, "new.py": 1}, {"a.py": 3})
        assert problem.startswith("GREW  new.py: 1") and "starts at zero" in problem

    def test_a_paydown_must_lower_the_baseline(self):
        """Otherwise the freed headroom lets the next change add calls back."""
        assert guard.compare({"a.py": 1}, {"a.py": 3})[0].startswith("STALE a.py")
        assert guard.compare({}, {"a.py": 3})[0].startswith("STALE a.py")


@pytest.fixture(scope="module")
def live_scan():
    """One parse of the backend for the whole module."""
    return guard.scan_tree()


class TestTheLiveTree:
    def test_the_scan_finds_the_class_it_exists_for(self, live_scan):
        """Not a pin on any one function — those are what #4674 fixes, and a
        test naming them would fail the day they are fixed. Only that the scan
        of this checkout sees both families, and reaches some of them through
        a module's own sync function."""
        kinds = {kind for found in live_scan.values() for _, _, kind in found}
        assert {"db", "docker"} <= kinds
        assert any(kind.startswith("via:") for kind in kinds)

    def test_the_private_submodule_and_migrations_are_out_of_scope(self, live_scan):
        assert not any("/enterprise/" in p or "/migrations/" in p for p in live_scan)

    def test_the_ratchet(self, live_scan):
        """THE guard. A failure here means a diff added a blocking call to an
        `async def` (hop to a thread, or make the handler a plain `def`), or
        removed some without regenerating the baseline:
            python scripts/ci/check_sync_on_loop.py --write"""
        problems = guard.compare(guard.counts(live_scan), guard.load_baseline())
        assert problems == [], "\n" + "\n".join(problems)
