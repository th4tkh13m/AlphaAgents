"""Refresh Selection-only plots and reports without touching experiment state."""

import argparse
import datetime
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from alpha_agents.tree_search.bridges.androidworld.task_outcomes import (  # noqa: E402
    is_bounded_agent_failure,
)


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def outcome(episode):
    if is_bounded_agent_failure(episode):
        return "bounded zero"
    score, reward = episode.get("success"), episode.get("androidworld_reward")
    valid = all(
        type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
        for v in (score, reward)
    ) and not episode.get("exception")
    valid = valid and episode.get("artemis_status") in {"completed", "failed"}
    if not valid or score != (
        reward if episode["artemis_status"] == "completed" else 0
    ):
        return "invalid"
    return "pass" if score > 0.5 else "partial" if score > 0 else "zero"


def collect(directory, metadata):
    run = directory / "attempt_001"
    state_bytes = (run / "state.json").read_bytes()
    state = json.loads(state_bytes)
    tasks = [r for r in read(metadata)["rows"] if r["stage"] in {"Screen", "Selection"}]
    records = dict(state["records"])
    records.update(
        {
            p["id"]: {"parent_id": p["parent_id"], "status": "running"}
            for p in state.get("pending", [])
            if p["id"] not in records
        }
    )
    nodes = []
    for name, record in records.items():
        child = run / name
        panels, sources = {}, []
        for stage in ("screen", "selection"):
            rows = {}
            paths = sorted((child / "stages" / stage).glob("worker_*/manifest.json"))
            if not paths and (child / "stages" / stage / "manifest.json").exists():
                paths = [child / "stages" / stage / "manifest.json"]
            for path in paths:
                raw = path.read_bytes()
                manifest = json.loads(raw)
                sources.append(
                    {
                        "path": str(path.relative_to(directory)),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
                for episode in manifest.get("episodes", []):
                    task = episode["template"]
                    if task in rows:
                        raise ValueError(
                            f"Duplicate task evidence: {name}/{stage}/{task}"
                        )
                    rows[task] = {
                        "classification": outcome(episode),
                        "episode": episode,
                    }
            panels[stage] = rows
        evaluation = record.get("evaluation") or {}
        valid_score = (
            evaluation.get("score") if evaluation.get("status") == "completed" else None
        )
        nodes.append(
            {
                "id": name,
                "label": "Root"
                if name == "initial"
                else f"Child {int(name.split('_')[-1])}",
                "parent": record.get("parent_id"),
                "status": record["status"],
                "score": valid_score,
                "panels": panels,
                "sources": sources,
                "transfer": read(child / "transfer_report.json"),
                "assessment": read(child / "transfer_assessment.json"),
                "mutation": read(child / "mutation/process.json"),
                "validation": record.get("validation", []),
            }
        )
    return {
        "snapshot": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "state_sha256": hashlib.sha256(state_bytes).hexdigest(),
        "tasks": tasks,
        "nodes": nodes,
        "root_reuse": read(run / "initial/root_reuse.json"),
        "monitor": read(directory / "monitor_status.json"),
        "controller_alive": alive(directory)
        if (directory / "launch.json").exists()
        else None,
    }


def passed(rows):
    return sum(r["classification"] == "pass" for r in rows.values())


def publish(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def reports(directory, data):
    output = directory / "reports"
    output.mkdir(exist_ok=True)
    nodes = data["nodes"]
    intro = (
        f"Snapshot **{data['snapshot']}**. "
        + (
            "Controller stopped; terminal audit requires separate verification. "
            if data.get("controller_alive") is False
            else "Interim results while the experiment runs. "
        )
        + "Screen and Selection only; held-out Confirmation outcomes are excluded. "
        "Root outcomes are reused, not rerun.\n\n"
        "A full pass requires reward greater than 0.5. Partial rewards, bounded agent failures, "
        "invalid outcomes, pending tasks and tasks not run remain distinct. "
        "Recorded passes in an invalid evaluation are not a valid ranked score.\n\n"
        "[Tree and progress](../plots/tree_and_progress.png) · "
        "[Task matrix](../plots/task_completion_matrix.png) · "
        "[Difficulty chart](../plots/difficulty_task_completion.png)\n\n"
    )
    difficulty = [
        "# Task completion and difficulty\n\n",
        intro,
        "| Node | Screen /5 | Easy /8 | Medium /7 | Hard /5 | Selection recorded passes | Valid ranked score | Status |\n",
        "|---|---:|---:|---:|---:|---:|---:|---|\n",
    ]
    mutation = [
        "# Coding agent mutation and capability transfer\n\n",
        intro,
        "Transfer assessments measure target gain and parent-task preservation independently of aggregate score. "
        "Donor inspection is not proof of use or causality. Agent development claims remain separate from official evaluations. "
        "Version one records assessments without changing search admission.\n\n",
        "| Node | Parent | Target | Donors claimed used | Assessment |\n",
        "|---|---|---|---|---|\n",
    ]
    for node in nodes:
        selection = node["panels"]["selection"]
        counts = []
        for level in ("easy", "medium", "hard"):
            panel = [
                t["task"]
                for t in data["tasks"]
                if t["stage"] == "Selection" and t["difficulty"] == level
            ]
            counts.append(
                str(
                    sum(
                        selection.get(t, {}).get("classification") == "pass"
                        for t in panel
                    )
                )
                if selection
                else "Not run"
            )
        valid = f"{node['score']:.0%}" if node["score"] is not None else "—"
        screen = node["panels"]["screen"]
        screen_text = (
            f"{passed(screen)}/5"
            if screen
            else "Not rerun"
            if node["id"] == "initial"
            else "Not run"
        )
        selection_text = (
            f"{passed(selection)}/20 ({len(selection)} finalized)"
            if selection
            else "Pending"
            if node["status"] == "running"
            else "Not run"
        )
        difficulty.append(
            f"| {node['label']} | {screen_text} | {' | '.join(counts)} | "
            f"{selection_text} | {valid} | {node['status']} |\n"
        )
        transfer, assessment = node["transfer"], node["assessment"]
        mutation.append(
            f"| {node['label']} | {node['parent'] or '—'} | {transfer.get('target_task', '—')} | "
            f"{', '.join(transfer.get('donors_used', [])) or 'None documented'} | "
            f"{assessment.get('status', 'Pending' if node['id'] != 'initial' else 'Baseline')} |\n"
        )
    for node in nodes:
        selection = node["panels"]["selection"]
        difficulty.append(f"\n## {node['label']}\n\n")
        for category in ("pass", "partial", "zero", "bounded zero", "invalid"):
            names = [t for t, r in selection.items() if r["classification"] == category]
            difficulty.append(
                f"{category.capitalize()}: {', '.join(names) or 'None recorded'}.\n\n"
            )
        expected = [t["task"] for t in data["tasks"] if t["stage"] == "Selection"]
        missing = [t for t in expected if t not in selection]
        difficulty.append(
            f"{'Pending or not yet evaluated' if node['status'] == 'running' else 'Not run'}: "
            f"{', '.join(missing) or 'None'}.\n\n"
        )
        mutation.append(f"\n## {node['label']}\n\n")
        tr, assessment = node["transfer"], node["assessment"]
        mutation.append(
            f"Mechanism claimed by coding agent: {tr.get('mechanism', 'No transfer report')}.\n\n"
        )
        mutation.append(
            f"Changed files claimed: {', '.join(tr.get('changed_files', [])) or 'None documented'}.\n\n"
        )
        for key in ("gains", "preserved", "regressions", "reasons"):
            mutation.append(
                f"{key.capitalize()}: {', '.join(assessment.get(key, [])) or 'None recorded'}.\n\n"
            )
        for check in tr.get("development_checks", []):
            mutation.append(
                f"Agent-reported development check: `{check.get('command', '')}` — {check.get('result', '')}\n\n"
            )
        if tr.get("remaining_uncertainty"):
            mutation.append(
                f"Agent-reported uncertainty: {tr['remaining_uncertainty']}\n\n"
            )
        links = [f"[task evidence](../{source['path']})" for source in node["sources"]]
        if assessment:
            links.append(
                f"[transfer assessment](../attempt_001/{node['id']}/transfer_assessment.json)"
            )
        if tr:
            links.append(
                f"[transfer report](../attempt_001/{node['id']}/transfer_report.json)"
            )
        mutation.append("Evidence: " + " · ".join(links) + ".\n\n")
    mutation.append(
        "## Engineering validation caveat\n\n"
        "Configured changed-test validation uses the run's original interpreter configuration. "
        "Failures of that configured gate remain failures in official assessments, even where "
        "supplemental checks with the harness environment pass. The experiment configuration is unchanged.\n"
    )
    publish(output / "node_task_difficulty_analysis.md", "".join(difficulty))
    publish(output / "codex_mutation_analysis.md", "".join(mutation))
    publish(
        output / "node_task_difficulty_analysis.json", json.dumps(data, indent=2) + "\n"
    )


def plots(directory, data):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    output = directory / "plots"
    output.mkdir(exist_ok=True)
    nodes = data["nodes"]
    stamp = data["snapshot"][:19] + (
        " UTC · controller stopped"
        if data.get("controller_alive") is False
        else " UTC · interim"
    )

    def save(fig, name):
        fig.suptitle(stamp, fontsize=10)
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        for extension in ("png", "svg"):
            destination = output / f"{name}.{extension}"
            temp = output / f"{name}.tmp.{extension}"
            fig.savefig(temp, dpi=150, bbox_inches="tight")
            temp.replace(destination)
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(14, max(5, len(nodes) * 0.28)))
    positions = {}
    for index, node in enumerate(nodes):
        depth = positions.get(node["parent"], (0, 0))[0] + 1 if node["parent"] else 0
        positions[node["id"]] = (depth, -index)
        if node["parent"] in positions:
            x, y = positions[node["parent"]]
            axes[0].plot([x, depth], [y, -index], color="#aaa", zorder=0)
        label = node["label"] + (
            f"\n{node['score']:.0%}"
            if node["score"] is not None
            else "\n" + node["status"]
        )
        axes[0].text(
            depth,
            -index,
            label,
            ha="center",
            va="center",
            fontsize=8,
            bbox={
                "boxstyle": "round",
                "facecolor": "#dcefe5" if node["score"] is not None else "#eee",
            },
        )
    axes[0].set_xlim(-0.6, max(p[0] for p in positions.values()) + 0.8)
    axes[0].set_ylim(-len(nodes), 1)
    axes[0].axis("off")
    axes[0].set_title("Parent lineage and valid scores")
    best, xs, ys = 0, [], []
    for i, node in enumerate(nodes):
        if node["score"] is not None:
            axes[1].scatter(i, node["score"] * 100, color="#207567")
            best = max(best, node["score"])
        xs.append(i)
        ys.append(best * 100)
    axes[1].step(xs, ys, where="post", label="Best valid Selection score")
    axes[1].set(
        ylim=(0, 100),
        xlabel="Child index (0 = reused root)",
        ylabel="Selection pass percentage",
    )
    axes[1].legend()
    axes[1].grid(alpha=0.2)
    save(fig, "tree_and_progress")

    classes = [
        "not run",
        "pending",
        "invalid",
        "bounded zero",
        "zero",
        "partial",
        "pass",
    ]
    colors = [
        "#eeeeee",
        "#cbd5e1",
        "#8b5cf6",
        "#e89f58",
        "#dd6b6b",
        "#eac85b",
        "#48a78a",
    ]
    tasks = [t for t in data["tasks"] if t["stage"] == "Selection"]
    matrix = [
        [
            classes.index(
                node["panels"]["selection"]
                .get(t["task"], {})
                .get(
                    "classification",
                    "pending" if node["status"] == "running" else "not run",
                )
            )
            for t in tasks
        ]
        for node in nodes
    ]
    fig, ax = plt.subplots(figsize=(15, max(5, len(nodes) * 0.35 + 3)))
    ax.imshow(matrix, cmap=ListedColormap(colors), vmin=-0.5, vmax=6.5, aspect="auto")
    ax.set_xticks(
        range(len(tasks)),
        [t["task"] for t in tasks],
        rotation=65,
        ha="right",
        fontsize=8,
    )
    ax.set_yticks(range(len(nodes)), [n["label"] for n in nodes])
    ax.set_title(
        "Selection task outcomes (invalid stages do not establish ranked scores)"
    )
    ax.legend(
        handles=[
            Patch(color=color, label=label) for color, label in zip(colors, classes)
        ],
        loc="upper left",
        bbox_to_anchor=(1.01, 1),
        ncol=1,
        fontsize=8,
    )
    save(fig, "task_completion_matrix")

    fig, axes = plt.subplots(1, 3, figsize=(14, max(4, len(nodes) * 0.3)))
    for ax, level in zip(axes, ("easy", "medium", "hard")):
        panel = [t["task"] for t in tasks if t["difficulty"] == level]
        counts = [
            sum(
                n["panels"]["selection"].get(t, {}).get("classification") == "pass"
                for t in panel
            )
            for n in nodes
        ]
        ax.barh(range(len(nodes)), counts, color="#48a78a")
        ax.set_yticks(
            range(len(nodes)),
            [
                n["label"]
                + (
                    " (not run)"
                    if not n["panels"]["selection"]
                    else " (partial)"
                    if len(n["panels"]["selection"]) < len(tasks)
                    else " (invalid stage)"
                    if n["score"] is None
                    else ""
                )
                for n in nodes
            ],
        )
        ax.invert_yaxis()
        ax.set(
            xlim=(0, len(panel)),
            title=f"{level.capitalize()} (/{len(panel)})",
            xlabel="Recorded full passes",
        )
    save(fig, "difficulty_task_completion")


def alive(directory):
    launch = read(directory / "launch.json")
    try:
        fields = (
            Path(f"/proc/{launch['pid']}/stat").read_text().split(") ", 1)[1].split()
        )
        return fields[19] == launch["start_ticks"] and fields[0] != "Z"
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--metadata", type=Path, default=ROOT / "androidworld_sets_manifest.json"
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=120)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("Report interval must be at least 30 seconds")
    while True:
        data = collect(args.directory, args.metadata)
        reports(args.directory, data)
        plots(args.directory, data)
        print(
            json.dumps(
                {
                    "snapshot": data["snapshot"],
                    "nodes": len(data["nodes"]),
                    "reporter_pid": os.getpid(),
                }
            ),
            flush=True,
        )
        if not args.watch or not alive(args.directory):
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
