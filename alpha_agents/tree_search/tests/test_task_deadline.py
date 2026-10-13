"""Task timeout cleanup under the actual subprocess phase watchdog."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from alpha_agents.tree_search.bridges.androidworld.artemis import normalize_manifest
from alpha_agents.tree_search.bridges.androidworld.phase_watch import run_task_bounded
from alpha_agents.tree_search.infrastructure.process import execute


def test_completed_task_preserves_result_and_does_not_begin_cleanup_early():
    cleanup = []

    async def work():
        return {"answer": 42}

    assert asyncio.run(run_task_bounded(work(), 2, lambda: cleanup.append(True))) == {"answer": 42}
    assert cleanup == []


def test_agent_exception_is_preserved():
    failure = ValueError("actual agent error")

    async def work():
        raise failure

    with pytest.raises(ValueError) as raised:
        asyncio.run(run_task_bounded(work(), 2, lambda: None))
    assert raised.value is failure


def test_external_cancellation_drains_the_owned_agent_task():
    finalized = []

    async def work():
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(.01)
            finalized.append(True)

    async def run():
        wrapper = asyncio.create_task(run_task_bounded(work(), 5))
        await asyncio.sleep(.02)
        wrapper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapper

    asyncio.run(run())
    assert finalized == [True]


@pytest.mark.parametrize("behavior,expected_phase", [
    ("slow_cancel", None), ("suppress_cancel", None),
    ("stuck_cancel", "cleanup"), ("blocked_loop", "task"),
])
def test_real_watchdog_accepts_clean_timeout_but_kills_unfinished_work(tmp_path, behavior, expected_phase):
    # This uses the actual helper, atomic phase markers, subprocess supervisor,
    # bounded-failure classifier and manifest normalizer. No Android or model.
    script = tmp_path / "runner.py"
    script.write_text('''import asyncio, json, sys, time
from pathlib import Path
from alpha_agents.tree_search.bridges.androidworld.phase_watch import enter_phase, run_task_bounded
from alpha_agents.tree_search.bridges.androidworld.task_outcomes import bounded_agent_failure
out=Path(sys.argv[1]); behavior=sys.argv[2]; phases=out/'phases.json'
async def work():
    if behavior=='blocked_loop': time.sleep(10)
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        if behavior=='suppress_cancel': return 'pretend success'
        await asyncio.sleep(10 if behavior=='stuck_cancel' else .15)
        raise
async def run():
    enter_phase(phases,'startup'); enter_phase(phases,'task')
    try:
        await run_task_bounded(work(),1.4,lambda:enter_phase(phases,'cleanup'))
        raise AssertionError('timed-out task returned a result')
    except TimeoutError as error:
        ep={'template':'FixtureTask','index':0,'seed':42,'success':0.0,
            'exception':f'{type(error).__name__}: {error}'}
        ep.update(bounded_agent_failure(error,'agent_execution'))
        manifest={'status':'completed','model':'fixture','model_base_url':'http://fixture',
            'seed':42,'tasks':['FixtureTask'],'combinations':1,'cleanup_status':'completed','episodes':[ep]}
        (out/'manifest.json').write_text(json.dumps(manifest))
        enter_phase(phases,'finished')
asyncio.run(run())
''')
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3]))
    report, _ = execute([sys.executable, str(script), str(tmp_path), behavior], tmp_path,
                        tmp_path / "process", timeout=5, env=environment,
                        phase_path=tmp_path / "phases.json",
                        phase_limits={"startup": 2, "task": 1.4, "cleanup": .8})
    events = json.loads((tmp_path / "phases.json").read_text())["events"]
    if expected_phase:
        assert report["timed_out"] and report["timeout_phase"] == expected_phase
        assert report["returncode"] != 0
        assert not (tmp_path / "manifest.json").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(report["pid"], 0)
    else:
        assert report["returncode"] == 0 and not report["timed_out"]
        assert [e["phase"] for e in events] == ["startup", "task", "cleanup", "finished"]
        assert events[2]["entered"] - events[1]["entered"] < 1.4
        metadata = json.loads((tmp_path / "manifest.json").read_text())
        summary = normalize_manifest(metadata, ["FixtureTask"],
                                     {"model":"fixture", "base_url":"http://fixture", "seed":42}, report)
        assert summary["status"] == "completed", summary
        outcome = summary["task_results"][0]
        assert outcome["success"] is False
        assert outcome["androidworld_reward"] is None
        assert metadata["episodes"][0]["failure_kind"] == "task_timeout"
