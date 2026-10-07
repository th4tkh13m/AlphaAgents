"""Trusted local archive lookup; run directly or with python -m."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from alpha_agents.tree_search.archive.catalog import digest, reconstruct
from alpha_agents.tree_search.core.storage import read_json, write_json
from alpha_agents.tree_search.core.workspace import git


def run(catalog_path, command, *, node=None, task=None):
    catalog_path = Path(catalog_path).resolve()
    catalog = read_json(catalog_path)
    if catalog["schema"] != 1:
        raise ValueError("Unknown archive schema")
    requests = catalog_path.parent / "requests"
    write_json(
        requests / f"{time.time_ns()}-{os.getpid()}.json",
        {
            "command": command,
            "node": node,
            "task": task,
            "catalog_sha256": digest(catalog_path),
        },
    )
    if command == "query":
        if task not in catalog["compatibility"]["tasks"]:
            raise ValueError("Task is outside allowed Selection evidence")
        parent_passes = {
            t for t, r in catalog["parent_tasks"].items() if r["score"] > 0.5
        }
        return {
            "task": task,
            "donors": [
                {
                    "ref": ref,
                    "score": entry["score"],
                    "target_score": entry["tasks"][task]["score"],
                    "target_seed": entry["tasks"][task]["seed"],
                    "lost_parent_passes": sorted(
                        t for t in parent_passes if entry["tasks"][t]["score"] <= 0.5
                    ),
                }
                for ref, entry in sorted(catalog["entries"].items())
                if entry["tasks"].get(task, {}).get("score", 0) > 0.5
            ],
        }
    if node not in catalog["entries"]:
        raise ValueError("Node is not in the frozen archive")
    entry = catalog["entries"][node]
    if command == "inspect":
        return {k: v for k, v in entry.items() if k not in ("source", "patches")}
    namespace, identifier = node.split("/")
    if any(Path(p).name != p or p in (".", "..") for p in (namespace, identifier)):
        raise ValueError("Unsafe archive reference")
    destination = catalog_path.parent / "exports" / namespace / identifier
    if command == "export":
        if task not in entry["tasks"]:
            raise ValueError("Task is outside donor Selection evidence")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not (destination / "source").exists():
            temporary = Path(
                tempfile.mkdtemp(prefix=".export-", dir=destination.parent)
            )
            try:
                source_hash = reconstruct(entry, temporary / "source")
                write_json(
                    temporary / "source_manifest.json",
                    {
                        "ref": node,
                        "source_sha256": source_hash,
                        "lineage": [{"sha256": p["sha256"]} for p in entry["patches"]],
                    },
                )
                temporary.rename(destination)
            except BaseException:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise
        evidence = destination / "task" / task
        if (
            evidence.exists()
            and read_json(evidence / "evidence_manifest.json").get("schema") != 2
        ):
            # Preserve exports made by the earlier goal-text-only exporter.
            # A repeated request gets corrected evidence at a distinct path.
            evidence = evidence / "schema_2"
        if not evidence.exists():
            from alpha_agents.tree_search.bridges.androidworld.archive import (
                AndroidWorldArchive,
            )

            temporary = Path(tempfile.mkdtemp(prefix=".task-", dir=destination))
            AndroidWorldArchive.export_task(
                entry["tasks"][task], temporary / "evidence"
            )
            evidence.parent.mkdir(exist_ok=True)
            (temporary / "evidence").rename(evidence)
            temporary.rmdir()
        return {
            "source": str(destination / "source"),
            "evidence": str(evidence),
            "source_manifest": str(destination / "source_manifest.json"),
        }
    if command == "compare":
        source = destination / "source"
        if not source.exists():
            raise ValueError("Export donor before comparison")
        workspace = catalog_path.parent.parent
        patch = []
        base = read_json(catalog_path.parent / "parent.json")["base_commit"]
        current_names = git(
            workspace, "ls-tree", "-r", "--name-only", base
        ).splitlines()
        donor_names = git(source, "ls-files", "-z").split("\0")
        for name in sorted(set(current_names + donor_names) - {""}):
            right = source / name
            a = (
                subprocess.check_output(
                    ["git", "show", base + ":" + name], cwd=workspace
                )
                .decode(errors="replace")
                .splitlines(keepends=True)
                if name in current_names
                else []
            )
            b = (
                right.read_text(errors="replace").splitlines(keepends=True)
                if right.is_file()
                else []
            )
            patch.extend(
                difflib.unified_diff(
                    a, b, fromfile="parent/" + name, tofile="donor/" + name
                )
            )
        output = destination / "parent_to_donor.diff"
        output.write_text("".join(patch))
        return {
            "diff": str(output),
            "note": "Complete immutable parent and donor versions, independent of your edits",
        }
    raise ValueError("Unknown archive command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query")
    query.add_argument("--task", required=True)
    for command in ("inspect", "export", "compare"):
        item = sub.add_parser(command)
        item.add_argument("--node", required=True)
        if command == "export":
            item.add_argument("--task", required=True)
    args = parser.parse_args()
    result = run(
        args.catalog,
        args.command,
        node=getattr(args, "node", None),
        task=getattr(args, "task", None),
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
