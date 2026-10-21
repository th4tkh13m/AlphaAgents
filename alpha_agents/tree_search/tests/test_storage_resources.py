"""Storage savings must retain WAL records, scope, identity, and recovery."""
import errno
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alpha_agents.tree_search.core.mcts.artifacts import ArtifactStore, manifest
from alpha_agents.tree_search.core.mcts.resource_retention import (
    cleanup_sqlite_scratch,
    readonly_projection,
    retire_workspace,
    verification_files,
)
from alpha_agents.tree_search.core.mcts.task_cache import evidence_inventory
from alpha_agents.tree_search.infrastructure.evidence_sqlite import (
    DIRECTORY,
    inspection_catalog,
    prepare_inspection_databases,
)
from alpha_agents.tree_search.tests.test_mcts import engine_factory as _engine_factory


@pytest.fixture
def resource_engine_factory(tmp_path):
    return _engine_factory.__wrapped__(tmp_path)


def database(path, *, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE records(value TEXT)")
    connection.execute("INSERT INTO records VALUES('committed observation')")
    connection.commit()
    return connection


def test_sealed_snapshot_contains_wal_only_records_and_preserves_original_bytes(tmp_path):
    root = tmp_path / "evidence"
    source = root / "runtime/data_engine.db"
    with database(source, wal=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        originals = {p: p.read_bytes() for p in source.parent.iterdir()}
        with sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True) as main_only:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                main_only.execute("SELECT * FROM records")
        prepare_inspection_databases(root)
        assert all(p.read_bytes() == content for p, content in originals.items())
        inventory = evidence_inventory(root)
        entries = inspection_catalog(root, inventory)
        assert len(entries) == 1 and entries[0]["source_file"] == str(source)
        snapshot = Path(entries[0]["standalone_file"])
        with sqlite3.connect(entries[0]["read_uri"], uri=True) as inspection:
            assert inspection.execute("SELECT * FROM records").fetchall() == [("committed observation",)]
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                inspection.execute("DELETE FROM records")
        assert not Path(str(snapshot) + "-wal").exists()
        prepare_inspection_databases(root)
        assert evidence_inventory(root) == inventory
        assert all(p.read_bytes() == content for p, content in originals.items())


def test_parallel_publishers_share_one_snapshot_and_detect_corruption(tmp_path):
    root = tmp_path / "evidence"
    database(root / "trace.db").close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: prepare_inspection_databases(root), range(3)))
    assert len(list((root / DIRECTORY).glob("*.sqlite"))) == 1
    snapshot = next((root / DIRECTORY).glob("*.sqlite"))
    snapshot.write_bytes(b"corrupted snapshot")
    with pytest.raises(ValueError, match="bound|changed"):
        inspection_catalog(root, evidence_inventory(root))
    with pytest.raises(ValueError, match="Conflicting|changed"):
        prepare_inspection_databases(root)


def test_inspection_recovers_a_copied_hot_journal_without_touching_the_source(tmp_path):
    root = tmp_path / 'evidence'
    source = root / 'trace.db'
    connection = database(source)
    try:
        connection.execute('PRAGMA cache_size=1')
        connection.execute('UPDATE records SET value=zeroblob(1000000)')
        assert Path(str(source)+'-journal').exists()
        original = {p: p.read_bytes() for p in root.iterdir()}
        prepare_inspection_databases(root)
        entry = inspection_catalog(root, evidence_inventory(root))[0]
        with sqlite3.connect(entry['read_uri'], uri=True) as snapshot:
            assert snapshot.execute('SELECT value FROM records').fetchone()[0] == 'committed observation'
        assert all(p.read_bytes() == content for p, content in original.items())
    finally:
        connection.rollback()
        connection.close()


def test_historical_evidence_has_no_implicit_immutable_grant(tmp_path):
    root = tmp_path / "evidence"
    database(root / "legacy.db").close()
    assert inspection_catalog(root, evidence_inventory(root)) == []


def test_snapshot_cannot_hide_a_new_source_wal_or_forged_inspection_only_root(tmp_path):
    root = tmp_path / 'evidence'
    database(root / 'trace.db').close()
    prepare_inspection_databases(root)
    (root / 'trace.db-wal').write_bytes(b'new unaccounted records')
    with pytest.raises(ValueError, match='sidecars'):
        inspection_catalog(root, evidence_inventory(root))
    other = tmp_path / 'forged'
    (other / DIRECTORY).mkdir(parents=True)
    (other / DIRECTORY / 'index.json').write_text('{}')
    with pytest.raises(ValueError, match='no original'):
        prepare_inspection_databases(other)


def test_snapshot_publication_rejects_source_symlinks(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    database(tmp_path / "outside.db").close()
    (root / "trace.db").symlink_to(tmp_path / "outside.db")
    with pytest.raises(ValueError, match="symlink"):
        prepare_inspection_databases(root)


def test_shared_views_are_one_copy_and_writable_materializations_are_independent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "harness.py").write_text("value = 1\n")
    artifacts = ArtifactStore(tmp_path / "run/artifacts")
    artifact = artifacts.freeze(source)
    with ThreadPoolExecutor(max_workers=3) as pool:
        views = list(pool.map(lambda _: artifacts.readonly_view(artifact), range(3)))
    assert len(set(views)) == 1
    assert len([p for p in views[0].parent.iterdir() if p.is_dir()]) == 1
    writable = tmp_path / "implementation"
    artifacts.materialize(artifact, writable)
    (writable / "harness.py").write_text("value = 2\n")
    assert (views[0] / "harness.py").read_text() == "value = 1\n"
    assert artifacts.verify(artifact)
    (views[0] / "harness.py").write_text("corruption")
    with pytest.raises(ValueError, match="differs"):
        artifacts.readonly_view(artifact)
    assert artifacts.verify(artifact)


def test_evidence_projection_reuses_inodes_and_removal_keeps_original(tmp_path):
    import shutil
    root = tmp_path / "evidence"
    database(root / "trace.db").close()
    prepare_inspection_databases(root)
    expected = evidence_inventory(root)
    projection = tmp_path / "read-only-result"
    report = readonly_projection(root, projection)
    assert report["hardlinked_files"] > 0 and report["copied_files"] == 0
    assert evidence_inventory(projection) == expected
    assert (root / "trace.db").stat().st_ino == (projection / "trace.db").stat().st_ino
    shutil.rmtree(projection)
    assert evidence_inventory(root) == expected


def test_projection_reports_copy_fallback_without_changing_evidence(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "receipt.json").write_text('{}')
    def unsupported(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")
    monkeypatch.setattr(os, "link", unsupported)
    report = readonly_projection(root, tmp_path / "projection")
    assert report["copied_files"] == 1 and report["hardlinked_files"] == 0
    assert evidence_inventory(root) == evidence_inventory(tmp_path / "projection")


def test_scratch_cleanup_preserves_logs_and_skips_live_processes(tmp_path, monkeypatch):
    from alpha_agents.tree_search.core.mcts import resource_retention
    request = tmp_path / "request"
    source = request / "scratch/inspection/trace.db"
    database(source).close()
    (source.parent / "inspect.py").write_text("# reproducible inspection\n")
    (source.parent / "inspection.log").write_text("one observed record\n")
    monkeypatch.setattr(resource_retention, "reconcile_process", lambda *a, **k: False)
    cleanup_sqlite_scratch(request)
    assert source.exists()
    monkeypatch.setattr(resource_retention, "reconcile_process", lambda *a, **k: True)
    cleanup_sqlite_scratch(request)
    assert not source.exists()
    assert (source.parent / "inspect.py").exists() and (source.parent / "inspection.log").exists()
    assert json.loads((request / "scratch_retention.json").read_text())["status"] == "derived-sqlite-evicted"


def test_workspace_retirement_retains_verification_and_frozen_reconstruction(tmp_path):
    run = tmp_path / "run"
    workspace = run / "workspaces/implementation-test"
    workspace.mkdir(parents=True)
    (workspace / "harness.py").write_text("value = 1\n")
    (workspace / "other.py").write_text("unused source\n")
    log = workspace / ".pytest_cache/test.log"
    log.parent.mkdir()
    log.write_text("checks passed\n")
    result = {"verification": [{"evidence": [".pytest_cache/test.log", "harness.py"]}]}
    artifacts = ArtifactStore(run / "artifacts")
    artifact = artifacts.freeze(workspace)
    before = artifacts.verify(artifact)
    retire_workspace(run, workspace, artifacts, artifact, run / "attempts/retention.json",
                     verification_files(workspace, result))
    assert log.read_text() == "checks passed\n" and (workspace / "harness.py").exists()
    assert not (workspace / "other.py").exists()
    restored = run / "workspaces/restored"
    artifacts.materialize(artifact, restored)
    assert manifest(restored) == before


def test_retirement_rejects_uncheckpointed_changes_and_artifact_paths(tmp_path):
    run = tmp_path / "run"
    workspace = run / "workspaces/test"
    workspace.mkdir(parents=True)
    source = workspace / "harness.py"
    source.write_text("before")
    artifacts = ArtifactStore(run / "artifacts")
    artifact = artifacts.freeze(workspace)
    source.write_text("uncheckpointed change")
    with pytest.raises(ValueError, match="differs"):
        retire_workspace(run, workspace, artifacts, artifact, run / "retention.json")
    with pytest.raises(ValueError, match="disposable"):
        retire_workspace(run, artifacts.root / artifact / "content", artifacts, artifact, run / "retention.json")


def test_completed_trial_recovery_reuses_shared_views_without_new_model_calls(resource_engine_factory):
    store, executor, gateway, evaluator = resource_engine_factory(max_depth=1)
    try:
        trial = store.reserve_trial()
        first = executor.run(trial)
        calls = list(gateway.calls)
        assert executor.recover(trial) == first and gateway.calls == calls
        assert not list((store.directory / "workspaces").glob("s0-*"))
        assert not list((store.directory / "workspaces").glob("screen-*"))
        assert not list((store.directory / "workspaces").glob("proposer-*"))
        assert len(list((store.directory / "value_code_views").iterdir())) == 2
        assert store.db.execute("SELECT count(*) FROM backups").fetchone()[0] == 1
    finally:
        store.close()


def test_reconstructing_a_missing_view_reuses_the_validated_role_response(resource_engine_factory):
    import shutil
    store, executor, gateway, _ = resource_engine_factory(max_depth=1)
    try:
        executor.run(store.reserve_trial())
        row = store.db.execute("SELECT * FROM invocations WHERE purpose='proposal' ORDER BY started LIMIT 1").fetchone()
        request = json.loads((store.directory / 'role_requests' / row['id'] / 'request.json').read_text())
        shared = Path(request['workspace'])
        shutil.rmtree(shared)
        before = list(gateway.calls)
        result = executor._readonly_role(row['owner'], 'proposal', 'proposer', request['artifact_id'], request['context'])
        assert result['artifact_id'] == request['artifact_id']
        assert gateway.calls == before and shared.is_dir()
        assert store.db.execute('SELECT count(*) FROM role_reuses').fetchone()[0] > 0
    finally:
        store.close()


def test_implementation_with_live_owned_work_is_not_retired(resource_engine_factory, monkeypatch):
    from alpha_agents.tree_search.core.mcts import recovery
    store, executor, gateway, _ = resource_engine_factory(max_depth=1)
    invoke = gateway.invoke
    def with_background_work(request, workspace):
        if request.role == 'implementer':
            (workspace / 'unreferenced.bin').write_bytes(b'retained while owned work is active')
        return invoke(request, workspace)
    gateway.invoke = with_background_work
    monkeypatch.setattr(recovery, 'reconcile_process', lambda *args, **kwargs: False)
    try:
        executor.run(store.reserve_trial())
        execution = next((store.directory / 'attempts').glob('*/execution.json'))
        workspace = Path(json.loads(execution.read_text())['workspace'])
        assert (workspace / 'unreferenced.bin').exists()
        assert not (execution.parent / 'workspace_retention.json').exists()
    finally:
        store.close()


def test_interrupted_partial_engineering_workspace_is_not_retired(resource_engine_factory):
    store, executor, _, _ = resource_engine_factory(max_depth=1, partial_patch=True)
    try:
        executor.run(store.reserve_trial())
        execution = next((store.directory / 'attempts').glob('*/execution.json'))
        workspace = Path(json.loads(execution.read_text())['workspace'])
        assert workspace.is_dir() and (workspace / 'controller.py').read_text() == 'value = 1\n'
        assert not (execution.parent / 'workspace_retention.json').exists()
    finally:
        store.close()


def test_cleanup_preserves_internal_symlink_citations(tmp_path):
    run = tmp_path / 'run'
    workspace = run / 'workspaces/implementation-test'
    workspace.mkdir(parents=True)
    (workspace / 'source.py').write_text('value = 1\n')
    (workspace / 'alias.py').symlink_to('source.py')
    (workspace / 'unreferenced.txt').write_text('not required for inspection')
    artifacts = ArtifactStore(run / 'artifacts')
    artifact = artifacts.freeze(workspace)
    result = {'verification': [{'evidence': ['alias.py:1']}]}
    retire_workspace(run, workspace, artifacts, artifact, run / 'retention.json',
                     verification_files(workspace, result))
    assert (workspace / 'alias.py').read_text() == 'value = 1\n'
    assert not (workspace / 'unreferenced.txt').exists()


def test_projection_rejects_external_links(tmp_path):
    root = tmp_path / 'evidence'
    root.mkdir()
    (tmp_path / 'private').write_text('outside the evidence grant')
    (root / 'injected').symlink_to(tmp_path / 'private')
    with pytest.raises(ValueError, match='symlink'):
        readonly_projection(root, tmp_path / 'projection')


@pytest.mark.parametrize('broken_database', [False, True])
def test_cache_publication_exposes_the_same_verified_snapshot_to_all_roles(tmp_path, broken_database):
    from alpha_agents.tree_search.core.mcts.contracts import (
        CombinedScore,
        FixedPUCTConfig,
        Objective,
    )
    from alpha_agents.tree_search.core.mcts.controller import canonical_judge_context
    from alpha_agents.tree_search.core.mcts.ports import StageEvidence
    from alpha_agents.tree_search.core.mcts.task_cache import (
        EvidenceCorrupt,
        TaskCache,
        TaskIdentity,
        instance_seed,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.permissions import (
        role_permissions,
    )
    from alpha_agents.tree_search.mutators.coding_agents.codex.roles.prompts import (
        RoleRequest,
    )

    run = tmp_path / 'run'
    config = FixedPUCTConfig(screen_gate='execution_valid')
    objective = Objective('subset', 'rubric', 'protocol')
    cache = TaskCache(run, config, objective)
    identity = TaskIdentity('a'*64, 'TaskA', 42, 'model', 'evaluator', 'fixture', {'version': 'test'}, {'task': 1})
    cache.register('root', identity.artifact_id, 's0', (identity,))
    root = run / 'tasks' / identity.key / 'execution/evidence'
    root.mkdir(parents=True)
    execution = cache.begin(identity, 'device', root)
    database(root / 'runtime/trace.db').close()
    if broken_database:
        (root / 'runtime/broken.db').write_bytes(b'SQLite format 3\0' + b'not a valid database'*30)
    cache.finish(execution, {'task_name': 'TaskA', 'seed': instance_seed(42, 'TaskA'),
                            'success': True, 'evidence_root': str(root)}, None, .1, 's0')
    cached = cache.cached(identity)
    assert any(item['path'].endswith('/index.json') for item in cached['inventory'])
    stage = StageEvidence(identity.artifact_id, 's0', {'TaskA': True}, (str(root),))
    context = canonical_judge_context(stage, ('TaskA',))
    assert len(context['sqlite_inspection']) == 1 + int(broken_database)
    assert cached['success'] is True
    if broken_database:
        unavailable = next(item for item in context['sqlite_inspection'] if item['status'] == 'unavailable')
        assert unavailable['error'] and 'read_uri' not in unavailable
    with cache.store() as store:
        from dataclasses import asdict
        store.save_evaluation(stage.identity, identity.artifact_id, 's0', asdict(stage))
        store.add_root('root', CombinedScore(identity.artifact_id, stage.identity, 'unused-judge', objective, 1, .5))
        assert store.node_evidence('root')['sqlite_inspection'] == context['sqlite_inspection']
    workspace = run / 'code-view'
    workspace.mkdir()
    (workspace / 'source.py').write_text('value = 1\n')
    for role in ['judge_review', 'proposer', 'value']:
        request = RoleRequest(str(run), 'call-'+role, role, identity.artifact_id, 'model', 'low',
                              {'canonical_evidence': context})
        profile = role_permissions(request, workspace)
        assert profile['filesystem'][str(root)] == 'read'
        assert str(root.parent) not in profile['filesystem']
    standalone = Path(next(item for item in context['sqlite_inspection'] if item['status'] == 'ready')['standalone_file'])
    standalone.write_bytes(b'corrupted sealed snapshot')
    with pytest.raises(EvidenceCorrupt, match='changed'):
        cache.cached(identity)
