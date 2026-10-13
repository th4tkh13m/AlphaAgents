"""Single subprocess entry point for DGM mutation and MCTS Codex roles.

The ``role`` subcommand delegates native Plan/Goal and assessment execution to
``roles.adapter``. The existing DGM CLI reconstructs its supplied engineering
context and runs a persisted goal. Search, evaluation, artifact admission, and
backup remain owned by their controllers and bridges.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__:
    from .execution import (
        _collect_goal,
        _goal_snapshot,
        _preflight_launcher,
        _require_idle_thread,
        run_watched_goal,
    )
    from .execution import _resume_goal_operation as _resume_goal_operation
    from .sdk_serialization import to_jsonable
else:
    from execution import (
        _collect_goal,
        _goal_snapshot,
        _preflight_launcher,
        _require_idle_thread,
        run_watched_goal,
    )
    from execution import _resume_goal_operation as _resume_goal_operation
    from sdk_serialization import to_jsonable


DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_EFFORT = "low"
GOAL_CONTEXT_FILENAME = ".dgm_goal_context.md"
PARENT_EVIDENCE_DIRECTORY = ".dgm_parent_evidence"
PROMPT_TEMPLATE_PATH = Path(
    os.environ.get("DGM_PROMPT_TEMPLATE", Path(__file__).with_name("prompt.md"))
)


def _jsonable(value):
    return to_jsonable(value, legacy_strings=True)


def _git_evidence(git_dir: str, base_commit: str) -> dict:
    def git(*args):
        result = subprocess.run(
            ["git", "-C", git_dir, *args], check=False, capture_output=True, text=True
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    return {
        "base_commit": base_commit,
        "status": git("status", "--short"),
        "changed_files": git("diff", "--name-only", base_commit),
        "diff_stat": git("diff", "--stat", base_commit),
    }


def build_instruction(
    problem_statement: str,
    test_description: str | None,
    evidence_bundle: str | None = None,
) -> str:
    """Render the structured, version-controlled instruction for one child."""
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "{{PROBLEM_STATEMENT}}": problem_statement,
        "{{TEST_DESCRIPTION}}": test_description
        or "No additional test instructions were supplied.",
        "{{EVIDENCE_BUNDLE}}": evidence_bundle
        or "No task-level evidence bundle was available. Inspect the repository and preserve this uncertainty.",
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template


def write_goal_context(git_dir: str, instruction: str) -> Path:
    """Place full child evidence in an ignored file readable by Codex."""
    worktree = Path(git_dir)
    context_path = worktree / GOAL_CONTEXT_FILENAME
    context_path.write_text("# DGM child context\n\n" + instruction, encoding="utf-8")

    git_pointer = worktree / ".git"
    git_dir_path = git_pointer
    if git_pointer.is_file():
        pointer = git_pointer.read_text(encoding="utf-8").strip()
        if pointer.startswith("gitdir: "):
            git_dir_path = Path(pointer.removeprefix("gitdir: "))
    exclude_path = git_dir_path / "info" / "exclude"
    if exclude_path.parent.exists():
        existing = (
            exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        )
        ignored = {GOAL_CONTEXT_FILENAME, f"{PARENT_EVIDENCE_DIRECTORY}/"}
        missing = [entry for entry in ignored if entry not in existing.splitlines()]
        if missing:
            with exclude_path.open("a", encoding="utf-8") as exclude:
                if existing and not existing.endswith("\n"):
                    exclude.write("\n")
                for entry in missing:
                    exclude.write(f"{entry}\n")
    return context_path


def build_goal_objective(context_path: Path) -> str:
    """Render the external persisted-goal objective with its context filename."""
    return Path(__file__).with_name("goal_objective.md").read_text(
        encoding="utf-8"
    ).format(context_filename=context_path.name)


def run_goal_or_turn(thread, instruction: str, git_dir: str, effort: str):
    """Run a persisted Codex goal, falling back only for older SDK shims."""
    client = getattr(thread, "_client", None)
    if client is not None and hasattr(client, "start_goal_operation"):
        # ``thread/goal/set`` is the SDK equivalent of Codex's /goal command.
        # It may span multiple physical turns; collect it as one logical result.
        state, turn_id = client.start_goal_operation(thread.id, instruction)
        return _collect_goal(client, state, turn_id), "goal"

    # Test doubles and older SDK releases do not expose persisted goals.
    if hasattr(thread, "turn"):
        return thread.turn(
            instruction, cwd=git_dir, effort=effort
        ).run(), "turn_compatibility"
    return thread.run(instruction, cwd=git_dir, effort=effort), "turn_compatibility"







def _record_recovery(telemetry_file: str | None, event: dict) -> None:
    if not telemetry_file:
        return
    path = Path(telemetry_file).with_name("codex_recovery.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, default=str) + "\n")



def run_codex(
    *,
    problem_statement: str,
    git_dir: str,
    chat_history_file: str,
    test_description: str | None,
    evidence_bundle: str | None,
    model: str,
    telemetry_file: str | None = None,
) -> str:
    """Run the user-authorized mutator without sandbox or approval interruptions."""
    history_path = Path(chat_history_file)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    instruction = build_instruction(
        problem_statement, test_description, evidence_bundle
    )
    history_path.write_text(
        "# Codex self-improvement turn\n\n## Prompt\n\n" + instruction
    )

    try:
        from openai_codex import ApprovalMode, Codex, Sandbox
    except ImportError as exc:
        raise RuntimeError(
            "Codex SDK is unavailable. Install the repository requirements so "
            "the openai-codex package is present, then authenticate once with `codex login`."
        ) from exc

    context_path = write_goal_context(git_dir, instruction)
    goal_objective = build_goal_objective(context_path)
    effort = os.environ.get("DGM_CODEX_EFFORT", DEFAULT_EFFORT)

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    thread_id = None
    recovery_events = []
    for attempt in range(3):
        # Every attempt owns a new app-server. A resumed attempt reopens the
        # same persisted conversation and the same stored goal.
        with Codex() as codex:
            if thread_id is None:
                thread = codex.thread_start(
                    cwd=git_dir,
                    model=model,
                    config={"model_reasoning_effort": effort},
                    sandbox=Sandbox.full_access,
                    approval_mode=ApprovalMode.deny_all,
                    ephemeral=False,
                )
                thread_id = thread.id
                _record_recovery(
                    telemetry_file, {"event": "thread_started", "thread_id": thread_id}
                )
                resume = False
            else:
                thread = codex.thread_resume(
                    thread_id,
                    cwd=git_dir,
                    model=model,
                    config={"model_reasoning_effort": effort},
                    sandbox=Sandbox.full_access,
                    approval_mode=ApprovalMode.deny_all,
                )
                if thread.id != thread_id:
                    raise RuntimeError("Codex resumed a different thread")
                goal_before = _goal_snapshot(thread._client, thread_id)
                if (
                    goal_before is None
                    or goal_before.status.value != "paused"
                    or goal_before.objective != goal_objective
                ):
                    raise RuntimeError("stored Codex goal changed before resume")
                _preflight_launcher(thread._client)
                _record_recovery(
                    telemetry_file,
                    {
                        "event": "launcher_preflight_passed",
                        "thread_id": thread_id,
                        "attempt": attempt,
                    },
                )
                resume = True

            if hasattr(thread, "_client") and hasattr(
                thread._client, "start_goal_operation"
            ):
                result, incident, goal_after = run_watched_goal(
                    thread, goal_objective, resume=resume
                )
                execution_mode = "goal"
            else:
                result, execution_mode = run_goal_or_turn(
                    thread, goal_objective, git_dir, effort
                )
                incident, goal_after = None, None
            thread_record = (
                thread.read(include_turns=True) if hasattr(thread, "read") else None
            )

            if incident is None:
                break
            event = {
                "event": "launcher_failure",
                "thread_id": thread_id,
                "attempt": attempt,
                "incident": incident,
                "goal_status": getattr(
                    getattr(goal_after, "status", None), "value", None
                ),
            }
            recovery_events.append(event)
            _record_recovery(telemetry_file, event)
            if goal_after is None or goal_after.status.value != "paused":
                raise RuntimeError(
                    "Codex launch failure detected, but goal pause was not verified"
                )
            _require_idle_thread(thread._client, thread_id)
            if attempt == 2:
                raise RuntimeError(
                    "Codex launcher failed persistently after two same-thread restarts"
                )

    response = result.final_response or "<Codex completed without a final response.>"
    telemetry = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "thread_id": thread_id,
        "turn_id": getattr(result, "id", None),
        "model": model,
        "effort": effort,
        "execution_mode": execution_mode,
        "recovery_events": recovery_events,
        "goal_status": getattr(getattr(goal_after, "status", None), "value", None),
        "goal_tokens_used": getattr(goal_after, "tokens_used", None),
        "goal_objective": goal_objective,
        "goal_context_file": str(context_path),
        "sandbox": "full_access",
        "approval_policy": "never",
        "turn_status": _jsonable(getattr(result, "status", None)),
        "duration_ms": getattr(result, "duration_ms", None),
        "usage": _jsonable(getattr(result, "usage", None)),
        "items": _jsonable(getattr(result, "items", [])),
        "thread_record": _jsonable(thread_record)
        if thread_record is not None
        else None,
        "git": _git_evidence(git_dir, base_commit="HEAD"),
    }
    if telemetry_file:
        telemetry_path = Path(telemetry_file)
        telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        telemetry_path.write_text(
            json.dumps(telemetry, indent=2, default=str) + "\n", encoding="utf-8"
        )
    with history_path.open("a") as history:
        history.write(
            f"\n\n## Thread\n\n{thread_id}\n\n## Telemetry\n\n{telemetry_file or 'not persisted'}\n\n## Final response\n\n{response}\n"
        )
    return response


def main() -> None:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "role":
        from alpha_agents.tree_search.mutators.coding_agents.codex.roles.adapter import (
            main as run_role,
        )
        run_role(arguments[1:])
        return
    parser = argparse.ArgumentParser(description="Codex SDK self-improvement worker.")
    parser.add_argument(
        "--context", required=True, help="Controller-owned mutation context JSON."
    )
    parser.add_argument("--git_dir", required=True)
    parser.add_argument("--chat_history_file", required=True)
    parser.add_argument(
        "--outdir", default="/dgm/", help="Directory for Codex execution telemetry."
    )
    parser.add_argument(
        "--model", default=os.environ.get("DGM_CODEX_MODEL", DEFAULT_MODEL)
    )
    args = parser.parse_args(arguments)

    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    evidence_bundle = json.dumps(context.get("evidence", {}))
    response = run_codex(
        git_dir=args.git_dir,
        problem_statement=context["objective"],
        chat_history_file=args.chat_history_file,
        test_description=context["instructions"]
        + "\nProtected paths: "
        + json.dumps(context.get("protected_paths", [])),
        evidence_bundle=evidence_bundle,
        model=args.model,
        telemetry_file=str(Path(args.outdir) / "codex_execution.json"),
    )
    print(response)


if __name__ == "__main__":
    main()
