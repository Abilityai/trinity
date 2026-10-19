"""#3722: persisted transport identity separates legacy recovery from lost config."""
import sqlite3
from unittest.mock import Mock

import pytest
from db_harness import db_backend, seed_user  # noqa: F401


def test_sqlite_upgrade_preserves_legacy_and_is_idempotent():
    from db.migrations import _migrate_agent_config_storage_format
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE agent_ownership (agent_name TEXT)')
        conn.execute("INSERT INTO agent_ownership VALUES ('legacy')")
        _migrate_agent_config_storage_format(conn.cursor(), conn)
        _migrate_agent_config_storage_format(conn.cursor(), conn)
        assert conn.execute('SELECT config_storage_format FROM agent_ownership').fetchone() == (None,)


def test_registration_records_transport_atomically_and_never_upgrades_existing(db_backend, monkeypatch):
    import db.agents as agents
    seed_user()
    users = Mock()
    users.get_user_by_username.return_value = {'id': 1}
    ops = agents.AgentOperations(users)
    assert ops.register_agent_owner('legacy', 'owner')
    assert ops.get_agent_config_storage_format('legacy') is None
    revision = None
    if db_backend == 'postgres':
        import importlib.util
        from pathlib import Path
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from db.engine import get_engine
        from sqlalchemy.exc import DBAPIError
        spec = importlib.util.spec_from_file_location('migration_3722', Path(__file__).parents[2] /
            'src/backend/migrations/versions/0101_agent_config_storage_format.py')
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        # Simulate an existing legacy database, then run the actual revision.
        with get_engine().begin() as conn:
            conn.exec_driver_sql('ALTER TABLE agent_ownership DROP COLUMN config_storage_format')
            monkeypatch.setattr(revision, 'op', Operations(MigrationContext.configure(conn)))
            revision.upgrade()
            revision.upgrade()
        assert ops.get_agent_config_storage_format('legacy') is None
    assert ops.register_agent_owner('new', 'owner', config_storage_format='volume-v1')
    assert ops.get_agent_config_storage_format('new') == 'volume-v1'
    if revision is not None:
        with pytest.raises(DBAPIError, match='Cannot downgrade'):
            with get_engine().begin() as conn:
                monkeypatch.setattr(revision, 'op', Operations(MigrationContext.configure(conn)))
                revision.downgrade()
        assert ops.get_agent_config_storage_format('new') == 'volume-v1'
    assert not ops.register_agent_owner('legacy', 'owner', config_storage_format='volume-v1')
    assert ops.get_agent_config_storage_format('legacy') is None
    with pytest.raises(ValueError):
        ops.register_agent_owner('unknown', 'owner', config_storage_format='future-format')
    assert ops.rename_agent('new', 'renamed')
    assert ops.get_agent_config_storage_format('renamed') == 'volume-v1'
    assert ops.get_volume_base_name('renamed') == 'new'
    assert ops.delete_agent_ownership('renamed')
    assert ops.get_agent_config_storage_format('renamed') == 'volume-v1'
    assert ops.purge_agent_ownership('renamed')
    with pytest.raises(ValueError, match='ownership'):
        ops.get_agent_config_storage_format('renamed')
    if revision is not None:
        # Once only legacy rows remain, downgrade and upgrade preserve them.
        with get_engine().begin() as conn:
            monkeypatch.setattr(revision, 'op', Operations(MigrationContext.configure(conn)))
            revision.downgrade()
            revision.upgrade()
        assert ops.get_agent_config_storage_format('legacy') is None


def test_migration_filenames_are_unique_and_published_history_resolves():
    """A merged graph alone does not catch a numeric filename collision."""
    from pathlib import Path
    from alembic.script import ScriptDirectory

    migrations = Path(__file__).parents[2] / 'src/backend/migrations'
    prefixes = [p.name.split('_', 1)[0] for p in (migrations / 'versions').glob('[0-9]*.py')]
    assert len(prefixes) == len(set(prefixes)), 'duplicate migration filename prefixes'
    scripts = ScriptDirectory(str(migrations))
    config = scripts.get_revision('0099_agent_config_storage_format')
    peer = scripts.get_revision('0099_agent_peer_skill_grants')
    merge = scripts.get_revision('0100_merge_agent_config_peer_grants')
    assert config is not None and peer is not None
    assert set(merge.down_revision) == {config.revision, peer.revision}
    assert len(scripts.get_heads()) == 1
    assert merge.revision in {r.revision for r in scripts.walk_revisions()}


def test_admin_email_and_config_histories_converge_without_rewriting_parents():
    from pathlib import Path
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory(str(Path(__file__).parents[2] / 'src/backend/migrations'))
    config_merge = scripts.get_revision('0100_merge_agent_config_peer_grants')
    admin = scripts.get_revision('0100_admin_email_not_username')
    convergence = scripts.get_revision('0103_merge_config_admin_email')
    assert admin.down_revision == '0099_agent_peer_skill_grants'
    assert set(config_merge.down_revision) == {
        '0099_agent_config_storage_format', '0099_agent_peer_skill_grants',
    }
    assert set(convergence.down_revision) == {config_merge.revision, admin.revision}
    assert len(scripts.get_heads()) == 1
    assert convergence.revision in {r.revision for r in scripts.walk_revisions()}
