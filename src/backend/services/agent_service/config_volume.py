"""Transport platform configuration through Docker, not backend host paths.

This module only provisions fresh configuration volumes. Existing volumes are
never regenerated or adopted by creation. Lifecycle callers own their identity.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import uuid

import docker

_IMAGE = "alpine:3.20"
_MAX_BYTES = 16 * 1024 * 1024
_CLAIM_LABEL = "trinity.config-creation"


class ConfigVolumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfigVolumeHandle:
    name: str
    claim: str


def _directory(archive, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o700
    info.uid = info.gid = 1000
    archive.addfile(info)


@contextmanager
def config_archive(config: Path, credentials: Path, seeds: Path | None):
    """Bounded archive of regular files only, with explicit private ownership."""
    files = [(Path(config), "agent-config.yaml"), (Path(credentials), "credentials.json")]
    directories = ["."]
    if seeds is not None:
        seeds = Path(seeds)
        if not stat.S_ISDIR(seeds.lstat().st_mode):
            raise ConfigVolumeError("Generated configuration must be a directory")
        directories.append("generated-creds")
        for path in sorted(seeds.rglob("*")):
            kind = path.lstat().st_mode
            name = "generated-creds/" + path.relative_to(seeds).as_posix()
            if stat.S_ISDIR(kind):
                directories.append(name)
            elif stat.S_ISREG(kind):
                files.append((path, name))
            else:
                raise ConfigVolumeError("Generated configuration contains a special file")
    total = 0
    with tempfile.TemporaryFile() as spool:
        with tarfile.open(fileobj=spool, mode="w") as archive:
            for name in directories:
                _directory(archive, name)
            for path, name in files:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError:
                    raise ConfigVolumeError("Configuration file unavailable") from None
                with os.fdopen(fd, "rb") as stream:
                    details = os.fstat(stream.fileno())
                    if not stat.S_ISREG(details.st_mode):
                        raise ConfigVolumeError("Configuration must contain regular files")
                    total += details.st_size
                    if total > _MAX_BYTES:
                        raise ConfigVolumeError("Configuration exceeds size limit")
                    info = tarfile.TarInfo(name)
                    info.size = details.st_size
                    info.mode = 0o600
                    info.uid = info.gid = 1000
                    archive.addfile(info, stream)
        spool.seek(0)
        yield spool


def remove_created_config_volume(client, handle: ConfigVolumeHandle):
    """Rollback only this attempt's fresh volume, never a mounted/adopted one."""
    try:
        volume = client.volumes.get(handle.name)
    except docker.errors.NotFound:
        return
    if (volume.attrs.get("Labels") or {}).get(_CLAIM_LABEL) != handle.claim:
        raise ConfigVolumeError("Configuration volume ownership changed")
    volume.remove(force=False)


def create_config_volume(client, agent_name, config, credentials, seeds):
    """Populate one new agent-config volume using the native archive interface."""
    from services.docker_utils import agent_volume_labels

    name = f"agent-{agent_name}-config"
    try:
        client.volumes.get(name)
    except docker.errors.NotFound:
        pass
    else:
        raise ConfigVolumeError("Existing configuration volume requires recovery")
    claim = uuid.uuid4().hex
    labels = agent_volume_labels(agent_name, "agent-config")
    labels[_CLAIM_LABEL] = claim
    handle = ConfigVolumeHandle(name, claim)
    with config_archive(config, credentials, seeds) as archive:
        volume = client.volumes.create(name=name, labels=labels)
        # Docker create is idempotent by name. A concurrent creator's existing
        # volume must never be initialized or rolled back by this attempt.
        if (volume.attrs.get("Labels") or {}).get(_CLAIM_LABEL) != claim:
            raise ConfigVolumeError("Configuration volume already claimed")
        helper = None
        try:
            try:
                client.images.get(_IMAGE)
            except docker.errors.ImageNotFound:
                client.images.pull(_IMAGE)
            helper = client.containers.create(
                _IMAGE, command=["true"], network_mode="none",
                labels=labels,
                volumes={name: {"bind": "/dest", "mode": "rw"}},
            )
            try:
                if not helper.put_archive("/dest", archive):
                    raise ConfigVolumeError("Configuration archive upload failed")
            finally:
                helper.remove(force=True)
        except Exception:
            # Also runs when helper teardown fails after a successful upload.
            # Never force removal: an attached helper keeps the volume safe for
            # operator recovery if the daemon cannot remove the container.
            try:
                remove_created_config_volume(client, handle)
            except Exception:
                pass  # Preserve the provisioning/teardown error.
            raise
    return handle
