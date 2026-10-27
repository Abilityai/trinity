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
}


def write_templates(root) -> None:
    for name, body in TEMPLATES.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "template.yaml").write_text(body)


def make_world(monkeypatch, tmp_path=None) -> World:
    """Users, the Docker fakes and the seams. Call inside a `db_backend` test."""
    for username, role in ((ADMIN, "admin"), (ALICE, "creator"), (BOB, "creator"), (CAROL, "user")):
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
    if tmp_path is not None:
        curated, deployed = tmp_path / "templates-curated", tmp_path / "templates-deployed"
        deployed.mkdir(parents=True, exist_ok=True)
        write_templates(curated)
        monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (curated.resolve(), deployed.resolve()))
        world.templates_root = curated
    return world
