"""The apply-mode test world (trinity-enterprise#695). Not a test module.

The REAL `db` over a full schema (`tests/db_harness.py::db_backend`), seeded
through the real writers. Docker is patched at its two read seams only —
`docker_service.agent_container_states` (the tri-state roster) and
`system_service._member_container` (the one per-member label read) — plus the
Docker leg inside `crud.agent_name_is_taken`, which reads the same fake.
`list_all_agents_fast` RAISES: apply must never depend on it (engineering F1).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, List, Optional

from database import db
from db_models import UserCreate
from models import User
from services import docker_service, system_service
from services.agent_service import crud

ADMIN, ALICE, BOB, CAROL = "admin695", "alice695", "bob695", "carol695"
SYSTEM = "acme"


class FakeContainer:
    """The shape the platform's predicates read: `attrs.Config.Labels`, `Mounts`."""

    def __init__(self, name: str, labels: Dict[str, str], mounts: List[dict], status: str):
        self.name = f"agent-{name}"
        self.id = f"c-{name}"
        self.status = status
        self.attrs = {"Config": {"Labels": dict(labels), "Env": []}, "Mounts": list(mounts)}

    @property
    def labels(self):
        return self.attrs["Config"]["Labels"]


def _mounts(name: str, expose: bool) -> List[dict]:
    mounts = [{"Destination": "/home/developer", "Type": "volume",
               "Name": f"agent-{name}-workspace", "RW": True}]
    if expose:
        mounts.append({"Destination": "/home/developer/shared-out", "Type": "volume",
                       "Name": f"agent-{name}-shared", "RW": True})
    return mounts


class FakeDocker:
    def __init__(self) -> None:
        self.containers: Dict[str, FakeContainer] = {}
        self.readable = True
        self.unreadable_labels: set = set()
        self.events: List[tuple] = []

    def states(self):
        if not self.readable:
            return None
        return {n: ("running" if c.status == "running" else "stopped")
                for n, c in self.containers.items()}

    def get(self, name: str):
        if name in self.unreadable_labels:
            return None
        return self.containers.get(name)

    def add(self, name: str, *, cpu: str = "2", memory: str = "4g", template: str = "",
            expose: bool = False, status: str = "running") -> FakeContainer:
        labels = {"trinity.platform": "agent", "trinity.agent-name": name,
                  "trinity.cpu": cpu, "trinity.memory": memory, "trinity.template": template}
        container = FakeContainer(name, labels, _mounts(name, expose), status)
        self.containers[name] = container
        return container

    async def start(self, name: str):
        """The platform's start path, modelled: a container whose stored config
        drifted is recreated with it (`start_agent_internal` ->
        `recreate_container_with_updated_config`), keeping its volumes."""
        self.events.append(("start", name))
        container = self.containers[name]
        labels = container.labels
        limits = db.get_resource_limits(name) or {}
        labels["trinity.cpu"] = limits.get("cpu") or labels["trinity.cpu"]
        labels["trinity.memory"] = limits.get("memory") or labels["trinity.memory"]
        folders = db.get_shared_folder_config(name)
        container.attrs["Mounts"] = _mounts(name, bool(folders and folders.expose_enabled))
        container.status = "running"
        return {"status": "started"}


@dataclass
class World:
    docker: FakeDocker
    users: Dict[str, User] = field(default_factory=dict)
    create_calls: list = field(default_factory=list)
    fail_create: set = field(default_factory=set)
    templates_root: Optional[object] = None

    def user(self, username: str) -> User:
        return self.users[username]

    def seed_agent(self, name: str, owner: str = ALICE, *, tags=(SYSTEM,), container: bool = True,
                   cpu: str = "2", memory: str = "4g", template: str = "local:plain",
                   status: str = "running") -> None:
        db.register_agent_owner(name, owner)
        if tags:
            db.set_agent_tags(name, list(tags))
        if container:
            self.docker.add(name, cpu=cpu, memory=memory, template=template, status=status)

    def snapshot(self, manifest_yaml: str, username: str = ALICE):
        manifest = system_service.parse_manifest(manifest_yaml)
        system_service.validate_manifest(manifest)
        return asyncio.run(system_service.read_apply_snapshot(manifest, self.user(username)))


def _raise_fast(*_a, **_k):
    raise AssertionError("apply must never read the roster through list_all_agents_fast")


# `local:` templates the fake create resolves through the REAL
# `crud._resolve_local_template` (so the template's own resources, shared
# folders and `schedules:` reach the member exactly as create applies them).
TEMPLATES = {
    "plain": "name: plain\n",
    # Declares a schedule a manifest re-declares (H3, P-F exception 1).
    "sched": (
        "name: sched\n"
        "schedules:\n"
        "  - name: daily\n"
        "    cron: '0 9 * * *'\n"
        "    message: /template-daily\n"
        "    description: from the template\n"
    ),
    # Overrides a declared resource (H4, P-F exception 2).
    "heavy": "name: heavy\nresources:\n  cpu: '4'\n  memory: '8g'\n",
    # Declares its own shared-folder settings (fresh review #2: `folders: {}`).
    "exposing": "name: exposing\nshared_folders:\n  expose: true\n",
}


def write_templates(root) -> None:
    for name, body in TEMPLATES.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "template.yaml").write_text(body)


def make_world(monkeypatch, tmp_path=None) -> World:
    """Users, the Docker fakes and the seams. Call inside a `db_backend` test."""
    # CAROL is an `operator`: past the Workspace floor, below `creator`.
    for username, role in ((ADMIN, "admin"), (ALICE, "creator"), (BOB, "creator"), (CAROL, "operator")):
        db.create_user(UserCreate(username=username, role=role, email=f"{username}@example.com"))
    docker = FakeDocker()
    world = World(docker=docker)
    for username in (ADMIN, ALICE, BOB, CAROL):
        row = db.get_user_by_username(username)
        world.users[username] = User(id=row["id"], username=username, role=row["role"],
                                     email=row.get("email"))
    monkeypatch.setattr(docker_service, "agent_container_states", docker.states)
    monkeypatch.setattr(system_service, "_member_container", docker.get)
    monkeypatch.setattr(crud, "get_agent_by_name",
                        lambda n: SimpleNamespace(name=n) if n in docker.containers else None)
    monkeypatch.setattr(docker_service, "list_all_agents_fast", _raise_fast)
    # `get_accessible_agents` reaches it through a by-name import, so patching
    # the module attribute alone would leave that path silently working.
    from services.agent_service import helpers as _helpers
    monkeypatch.setattr(_helpers, "list_all_agents_fast", _raise_fast)
    if tmp_path is not None:
        curated, deployed = tmp_path / "templates-curated", tmp_path / "templates-deployed"
        deployed.mkdir(parents=True, exist_ok=True)
        write_templates(curated)
        monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (curated.resolve(), deployed.resolve()))
        world.templates_root = curated
    return world


# ---------------------------------------------------------------------------
# The executor's seams: create, start, the lock's Redis. And the witnesses.
# ---------------------------------------------------------------------------

class LockRedis:
    """The apply lock's Redis: SET NX EX / GET / EXPIRE / DELETE and a
    `register_script` that executes the script TEXT (fakeredis here has no
    Lua). Every command is recorded."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.commands: List[tuple] = []
        self.on_refresh = None  # a hook a test uses to lose the lease mid-run

    def register_script(self, src: str):
        """EXECUTES the production script text (`_ent695_lua`), so the lock's
        ownership compare is the script's own, not a re-implementation."""
        import _ent695_lua as lua

        is_delete = "'del'" in src

        def _run(keys=None, args=None, client=None):
            target = client if client is not None else self
            key = (args or [None])[0]
            target.commands.append(("script", "release" if is_delete else "refresh", key))
            if not is_delete and target.on_refresh is not None:
                target.on_refresh(target, key)
            return lua.run(src, target.store, target.ttls, keys, args)

        return _run

    def set(self, key, value, nx=False, ex=None):
        self.commands.append(("set", key))
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key):
        self.commands.append(("get", key))
        return self.store.get(key)

    def expire(self, key, ttl):
        self.commands.append(("expire", key))
        return key in self.store

    def delete(self, key):
        self.commands.append(("delete", key))
        return 1 if self.store.pop(key, None) is not None else 0


def install_executor_seams(world: "World", monkeypatch) -> None:
    """The create seam registers ownership in the REAL DB, adds the container,
    records the `AgentConfig` it was handed, resolves `local:` templates through
    the REAL resolver and materializes the template's `schedules:` through the
    REAL reader and `crud.reconcile_declared_schedules` (engineering F9)."""
    from fastapi import HTTPException

    import redis_breaker_util
    from services.agent_service.capabilities import normalize_cpu, normalize_memory
    from services.template_schedules import normalize_declared_schedules

    async def create(config, current_user, request=None, skip_name_sanitization=False):
        world.create_calls.append(config.model_copy(deep=True))
        if config.name in world.fail_create:
            raise HTTPException(status_code=502, detail="template clone failed for "
                                "https://user:secret-pat@github.example/org/repo")
        cfg = config.model_copy(deep=True)
        template_data, template_folders = {}, None
        if cfg.template and cfg.template.startswith("local:"):
            template_data, template_folders = crud._resolve_local_template(cfg)
        cpu = normalize_cpu((cfg.resources or {}).get("cpu"), "2")
        memory = normalize_memory((cfg.resources or {}).get("memory"), "4g")
        db.register_agent_owner(cfg.name, current_user.username)
        if template_folders:
            db.upsert_shared_folder_config(cfg.name, expose_enabled=template_folders["expose"],
                                           consume_enabled=template_folders["consume"])
        folders = db.get_shared_folder_config(cfg.name)
        world.docker.add(cfg.name, cpu=cpu, memory=memory, template=cfg.template or "",
                         expose=bool(folders and folders.expose_enabled))
        declared = normalize_declared_schedules(template_data.get("schedules"))
        if declared:
            crud.reconcile_declared_schedules(cfg.name, declared, current_user.username)
        return SimpleNamespace(name=cfg.name)

    world.create = create
    world.redis = LockRedis()
    monkeypatch.setattr(system_service, "get_breaker_redis", lambda: world.redis)
    redis_breaker_util._ATOMIC_LEASE_SCRIPTS.reset()

    # Docker writes nobody may make on an existing member (P-A): recorded.
    from services import docker_utils

    for fn in ("container_stop", "container_remove", "container_rename", "container_start"):
        if hasattr(docker_utils, fn):
            async def _recorder(*a, _fn=fn, **k):
                world.docker.events.append((_fn, a))
            monkeypatch.setattr(docker_utils, fn, _recorder)


def apply(world: "World", manifest_yaml: str, username: str = ALICE, *, dry_run: bool):
    return asyncio.run(system_service.apply_manifest(
        manifest_yaml, world.user(username), None, dry_run=dry_run,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))


# Request bookkeeping every authenticated request performs, outside "no
# writes" (P-B2): the MCP key's usage counter.
BOOKKEEPING_PREFIXES = ("UPDATE mcp_api_keys SET last_used_at",)


class Witness:
    """Proves "no write to fleet state" through four independent channels.

    1. SQL: a `before_cursor_execute` listener records every INSERT / UPDATE /
       DELETE / REPLACE statement, so an identical-value update or a
       rolled-back write is still caught; bookkeeping is split out.
    2. Every table dumped before and after (rows as sorted tuples).
    3. Redis: the lock client's command log; Docker: the create/start/stop
       recorders.
    4. The filesystem: every non-database file under `data_root` digested by
       content and metadata, plus a recorder of every file-mutation call made
       while the witness is open, so an overwrite or a write-then-revert cannot
       pass.
    """

    _MUTATORS = (("os", "replace"), ("os", "rename"), ("os", "remove"), ("os", "unlink"),
                 ("os", "mkdir"), ("os", "makedirs"), ("os", "rmdir"),
                 ("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"),
                 ("shutil", "copytree"), ("shutil", "move"), ("shutil", "rmtree"))
    _PATH_MUTATORS = ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rename",
                      "replace", "rmdir")

    def __init__(self, world: "World", data_root) -> None:
        self.world = world
        self.data_root = data_root
        self.statements: List[str] = []
        self.fs_calls: List[tuple] = []
        self._patches: List[tuple] = []

    # -- SQL ---------------------------------------------------------------
    def _on_sql(self, conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            self.statements.append(" ".join(statement.split()))

    @property
    def fleet_statements(self) -> List[str]:
        return [s for s in self.statements if not s.startswith(BOOKKEEPING_PREFIXES)]

    @property
    def bookkeeping_statements(self) -> List[str]:
        return [s for s in self.statements if s.startswith(BOOKKEEPING_PREFIXES)]

    @staticmethod
    def dump_tables() -> Dict[str, list]:
        from sqlalchemy import inspect as sa_inspect, text
        from db.engine import get_engine
        engine = get_engine()
        out = {}
        with engine.connect() as conn:
            for table in sorted(sa_inspect(engine).get_table_names()):
                rows = conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
                out[table] = sorted(tuple(str(v) for v in row) for row in rows)
        return out

    # -- filesystem --------------------------------------------------------
    def _digest(self) -> Dict[str, tuple]:
        import hashlib
        import os
        out = {}
        for dirpath, _dirs, files in os.walk(self.data_root):
            for name in files:
                if name.startswith("trinity.db"):
                    continue  # the SQL witness covers the database files
                path = os.path.join(dirpath, name)
                st = os.stat(path)
                with open(path, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                out[path] = (digest, st.st_size, st.st_mode, st.st_mtime_ns)
        return out

    def _patch(self, owner, attr, wrapper):
        original = getattr(owner, attr)
        self._patches.append((owner, attr, original))
        setattr(owner, attr, wrapper(original))

    def _record(self, label):
        def wrapper(original):
            def recorded(*a, **k):
                self.fs_calls.append((label, a[:1]))
                return original(*a, **k)
            return recorded
        return wrapper

    def __enter__(self) -> "Witness":
        import builtins
        import importlib
        import pathlib
        from sqlalchemy import event
        from db.engine import get_engine

        self.tables_before = self.dump_tables()
        self.fs_before = self._digest()
        self.redis_mark = len(getattr(self.world, "redis", SimpleNamespace(commands=[])).commands)
        self.create_mark = len(self.world.create_calls)
        self.docker_mark = len(self.world.docker.events)
        self._engine = get_engine()
        event.listen(self._engine, "before_cursor_execute", self._on_sql)

        def open_wrapper(original):
            def recorded(file, mode="r", *a, **k):
                if any(c in mode for c in "wax+"):
                    self.fs_calls.append(("open", (file, mode)))
                return original(file, mode, *a, **k)
            return recorded
        self._patch(builtins, "open", open_wrapper)
        for module, attr in self._MUTATORS:
            self._patch(importlib.import_module(module), attr, self._record(f"{module}.{attr}"))
        for attr in self._PATH_MUTATORS:
            self._patch(pathlib.Path, attr, self._record(f"Path.{attr}"))
        return self

    def __exit__(self, *exc) -> None:
        from sqlalchemy import event
        for owner, attr, original in reversed(self._patches):
            setattr(owner, attr, original)
        event.remove(self._engine, "before_cursor_execute", self._on_sql)
        self.tables_after = self.dump_tables()
        self.fs_after = self._digest()

    # -- verdicts ----------------------------------------------------------
    @property
    def redis_commands(self) -> List[tuple]:
        return getattr(self.world, "redis", SimpleNamespace(commands=[])).commands[self.redis_mark:]

    @property
    def creates(self) -> list:
        return self.world.create_calls[self.create_mark:]

    @property
    def docker_events(self) -> list:
        return self.world.docker.events[self.docker_mark:]

    def changed_tables(self, ignore=("mcp_api_keys",)) -> List[str]:
        return [t for t in self.tables_after
                if t not in ignore and self.tables_after[t] != self.tables_before.get(t)]

    def assert_no_fleet_write(self) -> None:
        assert self.fleet_statements == [], self.fleet_statements
        assert self.changed_tables() == [], self.changed_tables()
        assert self.redis_commands == [], self.redis_commands
        assert self.creates == [] and self.docker_events == []
        assert self.fs_after == self.fs_before
        assert self.fs_calls == [], self.fs_calls
