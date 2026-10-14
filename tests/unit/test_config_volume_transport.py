"""Configuration transport must not depend on backend/daemon path identity."""
import io
from pathlib import Path
import tarfile

import pytest

from services.agent_service import config_volume


def test_archive_preserves_config_bytes_and_private_owner_modes(tmp_path):
    config = tmp_path / 'agent.yaml'
    credentials = tmp_path / 'credentials.json'
    config.write_bytes(b'agent:\n  custom_instructions: owner-specified\n')
    credentials.write_bytes(b'{}')
    seeds = tmp_path / 'seeds'
    seeds.mkdir()
    (seeds / '.env').write_bytes(b'SYNTHETIC=\n')
    with config_volume.config_archive(config, credentials, seeds) as archive:
        with tarfile.open(fileobj=archive) as tar:
            assert tar.extractfile('agent-config.yaml').read() == config.read_bytes()
            assert tar.extractfile('generated-creds/.env').read() == b'SYNTHETIC=\n'
            for member in tar:
                assert member.uid == member.gid == 1000
                assert member.mode == (0o700 if member.isdir() else 0o600)


def test_archive_rejects_symlink_instead_of_reading_another_file(tmp_path):
    actual = tmp_path / 'actual'
    actual.write_bytes(b'synthetic')
    link = tmp_path / 'config'
    link.symlink_to(actual)
    with pytest.raises(config_volume.ConfigVolumeError):
        with config_volume.config_archive(link, actual, None):
            pass


def test_existing_volume_is_never_overwritten_or_removed(monkeypatch, tmp_path):
    class Volumes:
        def get(self, name):
            return object()
        def create(self, **kwargs):
            pytest.fail('existing volume must not be overwritten')
    class Client:
        volumes = Volumes()
    with pytest.raises(config_volume.ConfigVolumeError):
        config_volume.create_config_volume(Client(), 'test-agent', tmp_path/'x', tmp_path/'y', None)


def _inputs(tmp_path):
    config, credentials = tmp_path/'config', tmp_path/'credentials'
    config.write_bytes(b'owner config')
    credentials.write_bytes(b'{}')
    return config, credentials


def test_concurrent_volume_claim_is_not_written_or_deleted(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import docker
    client = Mock()
    client.volumes.get.side_effect = docker.errors.NotFound('absent')
    foreign = Mock(attrs={'Labels': {'trinity.config-creation': 'other-attempt'}})
    client.volumes.create.return_value = foreign
    monkeypatch.setattr('services.docker_utils.agent_volume_labels', lambda *args: {})
    with pytest.raises(config_volume.ConfigVolumeError):
        config_volume.create_config_volume(client, 'ag1', *_inputs(tmp_path), None)
    client.containers.create.assert_not_called()
    foreign.remove.assert_not_called()


def test_failed_archive_removes_only_owned_unattached_volume(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import docker
    client = Mock()
    volume = Mock()
    def create(**kwargs):
        volume.attrs = {'Labels': kwargs['labels']}
        return volume
    client.volumes.create.side_effect = create
    client.volumes.get.side_effect = [docker.errors.NotFound('absent'), volume]
    helper = client.containers.create.return_value
    helper.put_archive.return_value = False
    monkeypatch.setattr('services.docker_utils.agent_volume_labels', lambda *args: {})
    with pytest.raises(config_volume.ConfigVolumeError):
        config_volume.create_config_volume(client, 'ag1', *_inputs(tmp_path), None)
    helper.remove.assert_called_once_with(force=True)
    volume.remove.assert_called_once_with(force=False)


def test_rollback_refuses_another_claim():
    from unittest.mock import Mock
    client = Mock()
    volume = client.volumes.get.return_value
    volume.attrs = {'Labels': {'trinity.config-creation': 'other'}}
    with pytest.raises(config_volume.ConfigVolumeError):
        config_volume.remove_created_config_volume(client, config_volume.ConfigVolumeHandle('agent-ag1-config', 'ours'))
    volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_missing_config_recovery_fails_before_credentials_change(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    import docker
    from fastapi import HTTPException
    from services.agent_service import lifecycle
    monkeypatch.setattr(lifecycle.db, 'get_agent_config_storage_format', lambda name: 'volume-v1')
    monkeypatch.setattr(lifecycle.db, 'get_volume_base_name', lambda name: 'original')
    lookup = AsyncMock(side_effect=docker.errors.NotFound('absent'))
    monkeypatch.setattr(lifecycle, 'volume_get', lookup)
    read_template = AsyncMock()
    monkeypatch.setattr(lifecycle, '_read_template_yaml_from_volume', read_template)
    with pytest.raises(HTTPException) as error:
        await lifecycle.recreate_missing_container('renamed')
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'AGENT_CONFIG_UNAVAILABLE'
    lookup.assert_awaited_once_with('agent-original-config')
    read_template.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_config_recovery_is_refused(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from fastapi import HTTPException
    from services.agent_service import lifecycle
    import services.docker_utils as docker_utils
    monkeypatch.setattr(lifecycle.db, 'get_agent_config_storage_format', lambda name: 'volume-v1')
    monkeypatch.setattr(lifecycle.db, 'get_volume_base_name', lambda name: 'original')
    volume = Mock(name='agent-original-config')
    volume.name = 'agent-original-config'
    volume.attrs = {'Labels': {'trinity.agent-name': 'another', 'trinity.platform': 'agent-config'}}
    monkeypatch.setattr(lifecycle, 'volume_get', AsyncMock(return_value=volume))
    monkeypatch.setattr(docker_utils, '_resolve_instance_id', lambda: 'test-instance')
    read_template = AsyncMock()
    monkeypatch.setattr(lifecycle, '_read_template_yaml_from_volume', read_template)
    with pytest.raises(HTTPException) as error:
        await lifecycle.recreate_missing_container('renamed')
    assert error.value.detail['code'] == 'AGENT_CONFIG_OWNER_MISMATCH'
    read_template.assert_not_awaited()


def test_persistent_staging_is_private_per_attempt_and_cleans_failure(monkeypatch, tmp_path):
    import shutil
    from models import AgentConfig
    from services.agent_service import crud
    monkeypatch.setattr(crud, '_get_default_resource', lambda field: '2' if field == 'cpu' else '4g')
    roots = []
    def stage_root(**kwargs):
        root = tmp_path / str(len(roots))
        root.mkdir(mode=0o700)
        roots.append(root)
        return str(root)
    monkeypatch.setattr(crud.tempfile, 'mkdtemp', stage_root)
    first = crud._stage_config_files(AgentConfig(name='same'), {}, None)
    second = crud._stage_config_files(AgentConfig(name='same'), {}, None)
    assert first[0].parent != second[0].parent
    assert first[0].parent.stat().st_mode & 0o777 == 0o700
    assert first[0].read_bytes() == second[0].read_bytes()
    monkeypatch.setattr(crud.yaml, 'dump', lambda *args: (_ for _ in ()).throw(OSError('synthetic write failure')))
    with pytest.raises(OSError):
        crud._stage_config_files(AgentConfig(name='same'), {}, None)
    assert not roots[-1].exists()
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize('reserved', [False, True])
def test_failed_create_reclaims_only_unregistered_config(monkeypatch, reserved):
    from unittest.mock import Mock
    from services.agent_service import crud
    client, remove = Mock(), Mock()
    monkeypatch.setattr(crud, 'docker_client', client)
    monkeypatch.setattr(crud, 'remove_created_config_volume', remove)
    monkeypatch.setattr(crud.db, 'is_agent_name_reserved', lambda name: reserved)
    monkeypatch.setattr(crud.snapshot_import, 'cleanup_staging', lambda path: None)
    handle = config_volume.ConfigVolumeHandle('agent-example-config', 'claim')
    handles = crud._RollbackHandles(agent_name='example', agent_mcp_key=None,
        git_instance_id=None, github_repo_for_agent=None,
        ephemeral_slot_reserved=False, ephemeral_owner_id=None, config_volume=handle)
    crud._cleanup_copy_artifacts(handles)
    if reserved:
        remove.assert_not_called()
    else:
        remove.assert_called_once_with(client, handle)


def test_helper_remove_failure_still_attempts_owned_volume_cleanup(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import docker
    client = Mock()
    volume = Mock()
    def create(**kwargs):
        volume.attrs = {'Labels': kwargs['labels']}
        return volume
    client.volumes.create.side_effect = create
    client.volumes.get.side_effect = [docker.errors.NotFound('absent'), volume]
    helper = client.containers.create.return_value
    helper.put_archive.return_value = True
    helper.remove.side_effect = docker.errors.APIError('synthetic remove failure')
    monkeypatch.setattr('services.docker_utils.agent_volume_labels', lambda *args: {})
    with pytest.raises(docker.errors.APIError):
        config_volume.create_config_volume(client, 'ag1', *_inputs(tmp_path), None)
    volume.remove.assert_called_once_with(force=False)


@pytest.mark.parametrize('name', ['x/../../outside', 'x/../../../outside'])
def test_3722_ephemeral_staging_rejects_escape_before_filesystem_write(monkeypatch, name):
    from unittest.mock import Mock
    from models import AgentConfig
    from services.agent_service import crud
    mkdir = Mock()
    legacy_mkdir = Mock(side_effect=AssertionError('legacy write reached'))
    monkeypatch.setattr(Path, 'mkdir', legacy_mkdir)
    monkeypatch.setattr(crud.os, 'makedirs', mkdir)
    with pytest.raises(ValueError, match='escaped its root'):
        crud._stage_config_files(AgentConfig(name=name, ephemeral={"ttl_seconds": 60}), {}, None)
    mkdir.assert_not_called()
    legacy_mkdir.assert_not_called()


def test_3722_generated_file_symlink_escape_is_refused(monkeypatch, tmp_path):
    from models import AgentConfig
    from services.agent_service import crud
    from fastapi import HTTPException
    root = tmp_path / 'private'
    root.mkdir()
    seeds = root / 'generated-creds'
    seeds.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('unchanged')
    (seeds / 'linked').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(crud.tempfile, 'mkdtemp', lambda **kwargs: str(root))
    monkeypatch.setattr(crud, 'generate_credential_files', lambda *a, **kw: {'linked/secret': 'replacement'})
    with pytest.raises(HTTPException):
        crud._stage_config_files(AgentConfig(name='contained'), {'name': 'synthetic'}, None)
    assert (outside / 'secret').read_text() == 'unchanged'
    assert not root.exists()


def test_3722_private_staging_accepts_nested_generated_files(monkeypatch, tmp_path):
    import shutil
    from models import AgentConfig
    from services.agent_service import crud
    root = tmp_path / 'private'
    root.mkdir()
    monkeypatch.setattr(crud.tempfile, 'mkdtemp', lambda **kwargs: str(root))
    monkeypatch.setattr(crud, '_get_default_resource', lambda field: '2' if field == 'cpu' else '4g')
    monkeypatch.setattr(crud, 'generate_credential_files', lambda *a, **kw: {'nested/config.json': '{}'})
    paths = crud._stage_config_files(AgentConfig(name='contained'), {'name': 'synthetic'}, None)
    assert paths[0].is_file() and paths[1].is_file()
    assert (root / 'generated-creds/nested/config.json').read_text() == '{}'
    shutil.rmtree(root)


@pytest.mark.parametrize('location', ['outside', 'wrong-prefix'])
def test_3722_cleanup_refuses_untrusted_directory(monkeypatch, tmp_path, location):
    from unittest.mock import Mock
    from services.agent_service import crud
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    target = (tmp_path / 'trinity-agent-config-outside' if location == 'outside'
              else temporary / 'unrelated')
    target.mkdir()
    sentinel = target / 'keep'
    sentinel.write_text('unchanged')
    monkeypatch.setattr(crud.tempfile, 'gettempdir', lambda: str(temporary))
    remove = Mock()
    monkeypatch.setattr(crud.shutil, 'rmtree', remove)
    with pytest.raises(ValueError):
        crud._cleanup_persistent_config_staging(target)
    remove.assert_not_called()
    assert sentinel.read_text() == 'unchanged'


def test_3722_cleanup_removes_valid_private_staging(monkeypatch, tmp_path):
    from services.agent_service import crud
    target = tmp_path / 'trinity-agent-config-owned'
    target.mkdir()
    (target / 'config.yaml').write_text('synthetic')
    monkeypatch.setattr(crud.tempfile, 'gettempdir', lambda: str(tmp_path))
    crud._cleanup_persistent_config_staging(target)
    assert not target.exists()


def test_3722_archive_helper_is_never_started_and_is_removed_after_upload(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import docker
    client = Mock()
    volume = Mock()
    def create(**kwargs):
        volume.attrs = {'Labels': kwargs['labels']}
        return volume
    client.volumes.create.side_effect = create
    client.volumes.get.side_effect = docker.errors.NotFound('absent')
    monkeypatch.setattr('services.docker_utils.agent_volume_labels', lambda *args: {})
    config_volume.create_config_volume(client, 'ag1', *_inputs(tmp_path), None)
    client.containers.create.assert_called_once()
    helper = client.containers.create.return_value
    helper.put_archive.assert_called_once()
    helper.start.assert_not_called()
    client.containers.run.assert_not_called()
    helper.remove.assert_called_once_with(force=True)
    assert 'restart_policy' not in client.containers.create.call_args.kwargs
    assert client.containers.create.call_args.kwargs['network_mode'] == 'none'
