"""Read-only real-archive verification plus root reuse; no emulator actions."""

import json
import tempfile
from pathlib import Path

from alpha_agents.tree_search.archive.catalog import digest
from alpha_agents.tree_search.archive.cli import run
from alpha_agents.tree_search.cli import build
from alpha_agents.tree_search.core.storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "runs/experiments/artemis_sol_medium_50_20261002T181115Z/attempt_005"


if __name__ == "__main__":
    config = read_json(OLD.parent / "config_full_benchmark_300s_3devices.json")
    config["harness"]["config"]["reuse_root_from"] = str(OLD)
    config["archive_access"] = {
        "enabled": True,
        "import_runs": [str(OLD)],
        "include_current_run": True,
        "allowed_stages": ["selection"],
    }
    output = Path(tempfile.mkdtemp(prefix="archive-transfer-preflight-"))
    controller = build(config, output)
    with controller.store.lock():
        controller._initialize(False)
        provider = controller.archive_provider
        descriptor = provider.snapshot(
            output, "preflight_child", "initial", dict(controller.state["records"])
        )
        from alpha_agents.tree_search.core.workspace import materialize

        candidate = materialize(
            controller.bridge.source,
            output,
            "preflight_child",
            controller.store.candidate("initial"),
        )
        with controller.bridge.lease() as resource:
            context = controller.bridge.prepare(
                candidate, controller.store.candidate("initial"), resource
            )
            context = provider.augment(candidate, context, descriptor)
        catalog = context.evidence["archive_access"]["catalog"]
        matches = run(catalog, "query", task="AudioRecorderRecordAudioWithFileName")
        ref = "import_00/child_000004"
        assert ref in [d["ref"] for d in matches["donors"]]
        export = run(
            catalog, "export", node=ref, task="AudioRecorderRecordAudioWithFileName"
        )
        compare = run(catalog, "compare", node=ref)
        assert Path(compare["diff"]).stat().st_size
        evidence = read_json(Path(export["evidence"]) / "evidence_manifest.json")
        assert evidence["images"] > 0, evidence
        before = digest(candidate.directory / "archive_snapshot.json")
        assert digest(Path(catalog)) == before
        report = {
            "status": "verified",
            "output": str(output),
            "root_reuse": read_json(output / "initial/root_reuse.json"),
            "donor": ref,
            "export": export,
            "comparison": compare,
            "evidence": evidence,
            "catalog_entries": len(read_json(Path(catalog))["entries"]),
            "emulator_actions": 0,
        }
        write_json(output / "preflight_report.json", report)
        print(json.dumps(report, indent=2))
