## 2026-10-20 — pattern — A harness that repairs shared state must restore what the test changed, never pick a side

**Context**: #3390. The 2026-08-10 and 2026-10-05 entries describe one class: a package attribute and its `sys.modules` entry left on two copies of one module, after an eviction tool restored only the entry. It recurred five times (#3272's 40 failures, #3470, #2572, trinity-enterprise#752, #2859), each fixed in its own polluter after a long bisect.

A whole-island probe found the class live in **64** test files, not the 5 the dossier listed. A guard in `tests/unit/conftest.py` now repairs it after every test (`tests/module_split_guard.py`).

The first version healed every split toward `sys.modules`, on the grounds that it is what the import system reads. That broke four `test_ent692_skill_sources_mcp` tests. `test_skill_service_user_agent.py` registers a private file-loaded copy of `services.skill_service` at collection, and `routers.skills` had already bound the canonical copy. Pointing the attribute at the private copy made `import services.skill_service as ss; monkeypatch.setattr(ss, "db", …)` patch a module the router never reads. Several other tests leave their own copy in `sys.modules` in the same way (`db.sessions`, `db.audit`).

**Lesson**:
1. A repair is safe only when it restores the exact pre-test state. Here that means re-pointing the attribute only while the `sys.modules` entry is still the object that was there before the test; the attribute's pre-test value is then known. When the entry itself moved, no rule can rank the two copies, so fail the test and say which module. Do not heal it.
2. Judge a moved entry once the file's module fixtures have torn down: `nextitem` in another module, the same boundary pytest uses. `test_fleet_status_resilience` holds a private `routers.monitoring` for its whole file and restores it in its own teardown. A per-test check called that a leak.
3. Leave alone anything that was already split before the first test (collection-time loaders, the #762 class). The harness cannot attribute it to a test, and it cannot heal it without picking a side.
4. Measure a repair's blast radius on the whole island before trusting a narrow probe. The 74-file span missed 59 of the 64 polluters.
5. Evidence that the guard is load-bearing: with the pre-#3468 polluter restored, the 4 victim files pass with the guard on and fail 41 with `TRINITY_SPLIT_GUARD=off`.
