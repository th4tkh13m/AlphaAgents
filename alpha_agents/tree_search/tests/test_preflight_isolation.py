"""Dependency imports cannot mutate the incoming harness's artifact identity."""
import io
import json
import sys
from types import SimpleNamespace

from alpha_agents.tree_search.bridges.androidworld.staged_evaluator import (
    EvaluationRevision,
    StagedArtemisEvaluator,
)
from alpha_agents.tree_search.core.mcts.artifacts import manifest


def test_preflight_redirects_import_time_trace_directories(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    package = source / 'artemis'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text(
        "import os\nfrom pathlib import Path\n"
        "root=Path(os.environ.get('ARTEMIS_TRACES_DIR',str(Path(__file__).parents[1]/'traces')))\n"
        "for name in ['images','replay/data','replay/outputs']: (root/name).mkdir(parents=True,exist_ok=True)\n"
    )
    benchmark = tmp_path / 'benchmark'
    (benchmark / 'android_world').mkdir(parents=True)
    (benchmark / 'android_world/__init__.py').write_text('')
    for name in ['adbutils', 'grpc']:
        (benchmark / (name + '.py')).write_text('')
    inherited = tmp_path / 'unrelated-runtime'
    monkeypatch.setenv('ARTEMIS_TRACES_DIR', str(inherited))
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps({'data': [{'id': 'model'}]}).encode()))
    adapter = StagedArtemisEvaluator.__new__(StagedArtemisEvaluator)
    adapter.config = {'python': sys.executable, 'adb_path': '/bin/true',
                      'androidworld_root': str(benchmark), 'base_url': 'http://unused', 'model': 'model'}
    adapter.cache = SimpleNamespace(directory=tmp_path / 'run')
    adapter.scheduler = SimpleNamespace(devices=())
    adapter.revision = EvaluationRevision('model', 'evaluator', 'fixture')
    adapter._verify_runtime_source = lambda: None
    before = manifest(source)
    for _ in range(2):
        result = adapter.preflight(source)
        assert json.loads(result['dependencies'])['artemis'] == str(package / '__init__.py')
        assert manifest(source) == before
    assert (tmp_path / 'run/preflight_runtime/replay/data').is_dir()
    assert not inherited.exists()
    assert not (source / 'traces').exists()
