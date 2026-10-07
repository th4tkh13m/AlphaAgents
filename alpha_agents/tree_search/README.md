# Harness-independent DGM search

DGM evolves a harness through code mutations and independent evaluations. The controller selects parents, schedules candidates, preserves lineage, and maintains scored, diagnostic, and retained archives. It has no Android, model SDK, task, or benchmark imports.

For a detailed explanation of parent selection, mutation, evaluation, archives, and scheduling, read [DGM tree search algorithm](DGM_ALGORITHM.md).

## Structure

```text
tree_search/
  core/               Contracts, controller, workspaces, and persistence
  infrastructure/     Shared process execution and command expansion
  bridges/
    command.py        Generic external evaluator and resource leases
    androidworld/     Mobile resources, benchmark execution, and recovery
  mutators/
    command.py        Arbitrary external editing command
    coding_agents/
      codex/          Backend, SDK worker, watcher, and prompt
  validators/         Optional regression checks
  cli.py              Configuration and implementation composition
  examples/           Offline harness and example configurations
  tests/              Search, boundary, backend, and recovery regressions
```

The actual harness source is supplied separately. It is copied into an immutable run snapshot; each candidate is reconstructed from that snapshot and its ancestor patches. Evaluator changes to a parent workspace do not become inherited source.

## Install and run

Requires Python 3.10+ and Git on PATH. From the `AlphaAgents` repository root:

```bash
python -m pip install -e '.[test]'
python -m alpha_agents.tree_search --config alpha_agents/tree_search/examples/command.json --output /tmp/dgm-demo
```

The offline example uses Python 3 (`python3` in the example commands). It starts with a score of 0 and makes one deterministic mutation that scores 1. The mutation script demonstrates the interface; it is not a learning agent. Use a fresh output directory for each new run.

For Codex, install `.[codex,test]`, authenticate with `codex login`, and use a Codex mutation configuration. `requirements.txt` also installs the Codex runtime and test dependency directly.

Use `--stop-after-children 1` to run the baseline and one child, then exit after that child's mutation and evaluation lifecycle has settled. The configured total child budget remains unchanged. Continue the same archive with `--resume`; combine it with `--stop-after-children 1` to run one more child at a time, or omit the stop flag to finish the remaining budget. The stop limit also caps parallel launches, and a clean stop leaves no pending child work. Failed mutations count as finished child lifecycles and remain recorded for diagnosis.

Codex mutation threads use user-authorized full-access execution with approval policy `never` for unattended candidate tests and assigned-device ADB commands. The SDK calls this approval mode `deny_all`: permission requests are disabled, while full access lets commands run without requesting escalation. Both fresh and resumed threads use these settings, and execution telemetry records them. Mutator instructions still limit edits to disposable candidate code and keep original harnesses, benchmark definitions, scoring, and parent evidence immutable; these instructions are not a filesystem sandbox.

For AndroidWorld, copy and edit `examples/androidworld.json`. Supply the external mobile-agent checkout, interpreter and dependencies, model endpoint, model name, and AndroidWorld endpoint explicitly. AndroidWorld requires Linux/WSL; the general controller and command bridge also support Windows.

Paths in a CLI config are relative to that config file. Existing local command script paths are resolved before execution. Command arguments are passed as an argument list without a shell. `{workspace}`, `{artifacts}`, and `{resource}` are supported evaluator placeholders; mutation commands support `{workspace}`, `{artifacts}`, and `{context}`. Literal braces must be doubled.

## Bridge API

Implement `HarnessBridge` in `core/contracts.py` and construct `DGMController(bridge, mutator, output, config, validators=())` directly:

1. `source`: the editable harness checkout to snapshot.
2. `identity()`: stable JSON configuration identifying the evaluator contract.
3. `lease()`: context manager owning a device, container, worker, or other resource; release it on success and failure.
4. `prepare(candidate, parent, resource)`: supply the mutation objective, instructions, evidence, and optional protected path globs. Attach dependencies or ignored context without changing tracked source.
5. `evaluate(candidate, resource)`: independently evaluate the candidate and return an `Evaluation`.


For a custom bridge or mutation backend, use the Python API without modifying the controller or CLI:

```python
from pathlib import Path
from alpha_agents.tree_search import DGMController, SearchConfig

controller = DGMController(
    my_bridge,
    my_mutator,
    Path("runs/experiment"),
    SearchConfig(max_children=20, workers=2),
)
state = controller.run()
```

A bridge owns task semantics, retries, scoring, and runtime cleanup. A mutation backend owns only code editing. It returns a `MutationOutcome`; the controller independently captures the Git patch, including new files.

Completed evaluations require a finite score in `[0, 1]`, with higher meaning better. Normalize other metrics inside the bridge (including reversing metrics where lower is better). A failed evaluator has no score. Every scored candidate must use the same benchmark contract. A bridge can flag diagnostic evidence even when evaluation succeeds.

The command evaluator writes exactly one JSON object to stdout, for example:

```json
{"status": "completed", "score": 0.75, "metrics": {"passed": 3, "total": 4}}
```

Failures use `{"status": "failed", "error": "runtime unavailable"}`. Logs go to stderr. The bridge saves stdout, stderr, and process metadata beside the candidate. Evaluation runs from a trusted directory outside the candidate, defaulting to the source's parent. Keep benchmark tasks and scoring outside editable source, or declare `protected_paths` globs. These path checks enforce the edit contract; they are not an isolation sandbox.

## Search and resume

### Archive-assisted capability transfer

Set `archive_access.enabled` to true to expose settled Selection evidence to
each coding agent. `import_runs` optionally supplies existing DGM run directories;
`include_current_run` defaults to true. Only local Artemis/AndroidWorld currently
implements the benchmark adapter. The coding agent chooses its task and donors;
existing parent selection and archive admission remain unchanged.

The context provides a trusted local helper for `query --task TASK`,
`inspect --node REF`, `export --node REF --task TASK`, and `compare --node REF`.
Exports reconstruct the complete baseline-plus-patch lineage and include
task-scoped trajectories and screenshots. Frozen catalogs, namespaced identities
and artifact checks keep each mutation's evidence consistent. Confirmation and
full-audit outcomes are excluded. These are integrity controls, not an OS sandbox.

The agent writes `transfer_report.json` in its candidate artifact directory.
Independent evaluation produces `transfer_assessment.json` with the target gain,
retained parent passes, regressions and evidence gaps. A positive assessment
requires observed target gain and preservation; it does not change the score or
search admission. No prescribed target or donor is inserted by the controller.

To reuse an already measured root, set
`harness.config.reuse_root_from` to the original run directory. Reuse validates
immutable source, canonical Selection manifests/seeds, agent/evaluator hashes,
dependency versions and task definitions against the existing audit identity.
A mismatch fails closed rather than silently rerunning the root. The root's
Selection evidence is copied; its full report remains outside mutation evidence.

Example optional configuration fragment:

```json
{
  "archive_access": {
    "enabled": true,
    "import_runs": ["../previous-run"],
    "include_current_run": true,
    "allowed_stages": ["selection"]
  },
  "transfer_assessment": {
    "require_partial_reward_preservation": false,
    "affect_search_admission": false
  }
}
```

Fresh paired verification panels and admission overrides are deliberately
rejected if configured: they are not silently treated as implemented.

`SearchConfig` controls the total child budget, worker count, batch size, scheduling, parent selection, archive policy, seed, and score tolerance. Synchronous search selects a batch from one parent pool and waits for all children before selecting another. Asynchronous search admits completed children and fills free slots immediately; completion order can affect parent choice.

`best` selects the highest score. `score_prop` uses normalized scores; `score_child_prop` additionally favors parents with fewer recorded children. Diagnostic evaluations remain selectable as repair evidence. Valid candidates below the archive threshold remain in the retained parent pool. `keep_better` admits scores at least baseline minus `score_tolerance`; `keep_all` admits every valid score.

A mutation error with a nonempty permitted patch still receives independent evaluation. An empty patch or a protected-file edit is a mutation failure. Runtime failures are diagnostic candidates, never a zero score.

Resume with the same config and output plus `--resume`. You may increase `max_children`; all other settings must match. `state.json` is the authoritative atomic snapshot and contains parent pools, lineage records, random state, and pending attempts. Each candidate also has its own `candidate.json`, `workspace.json`, patch, and evidence. Interrupted pending attempts are recovered from their saved base revision and preserved as diagnostic parents; they are not silently scored or rerun. One controller may own an output directory at a time. Interrupted initial setup can also resume: incomplete baseline directories are preserved separately before retrying initialization. The source snapshot is fingerprinted and checked on resume.

The CLI writes `search.log` in the output directory and also logs to stderr. Logs identify mutation and evaluation phases, subprocess IDs, elapsed time, exit codes, and timeouts. Subprocess `stdout.txt` and `stderr.txt` are written during execution, so they can be tailed while a candidate is running. Its `process.json` records the live PID and running status before execution completes, then the terminal result. Confirm that the recorded PID is still alive before interpreting an old running record as current activity. Artemis evaluation logs also identify each task as it starts and report its score on completion.

## AndroidWorld adapter

The mobile HTTP evaluator retains per-task checkpoints, device health/reset probes, episode reclamation, and evaluator crash recovery. A lease spans mutation and evaluation and is returned afterward. The bridge uses the new disjoint sets: 5 Screen, 20 Selection, and 91 held-out Confirmation templates. The default scoring split is `selection`; `evaluation` remains a compatibility alias for that split with the new manifest. `confirmation` requires an explicit fresh seed and cannot be used for mutation. Different subsets are never mixed in candidate ranking, and changing the task partition invalidates the resume contract. Local Artemis optionally performs cached full-benchmark audits of the root and final Selection winner, with Confirmation evidence stored outside parent directories. See the bridge README for full audits and parallel evaluation settings.

## Verification

```bash
python -m pip install -e '.[codex,test]'
python -m pytest alpha_agents/tree_search/tests -q
```

On Windows, AndroidWorld-specific tests are skipped. Tests cover actual offline harness processes and Git patch lineage, synchronous/asynchronous search, resume, partial mutation recovery, invalid evaluator results, resource leases, protected edits, archive policies, Codex worker behavior, and AndroidWorld task/episode crash recovery. Live AndroidWorld service execution and real Codex engineering goals require external credentials/resources and are separate integration checks.


## Structure and ownership

See [STRUCTURE_PLAN.md](STRUCTURE_PLAN.md) for the plan followed by this refactor.

The controller writes `mutation_context.json` once, captures the canonical `model_patch.diff`, persists validation reports, and decides candidate status. Mutators consume the context and edit source; their telemetry remains backend-specific. Bridges supply harness instructions and resources and perform independent benchmark evaluation. Infrastructure shares process execution and command expansion.

The coding-agent prompt requires observe, analyze, plan, implement, and validate. The agent validates through both relevant test cases and a representative harness test run, inspects the results, and reports any blockers. This is an agent responsibility; there is no live-evidence validator enforcing an experiment-summary file.

Validators are optional and configured independently. Add the following field to a full search configuration; `examples/validation.json` contains this configuration fragment:

```json
"validation": [
  {"type": "changed_pytest", "python": "/path/to/agent/python", "timeout": 300}
]
```

An absent validation list means no engineering checks are imposed. `changed_pytest` runs only changed existing Python test files. Validation establishes engineering evidence, not task success. Its failure flags diagnostic evidence and does not replace the benchmark score or block independent evaluation of a permitted patch.

Codex lives entirely in `mutators/coding_agents/codex/`. To add a dedicated Claude Code or other coding-agent backend, add a sibling package that implements `MutationBackend.identity()` and `mutate(candidate, context)`. Use the supplied context directly, or consume its controller-written JSON representation through `candidate.mutation_context`, launch the agent, and return `MutationOutcome`. Leave canonical patch capture to the controller. No controller or bridge changes are needed. Until a dedicated integration is implemented, `CommandMutator` can invoke any configured editing CLI with `{workspace}`, `{artifacts}`, and `{context}` arguments.

The Python API accepts custom bridges, mutators, and validators directly. CLI built-in implementations are composed in `cli.py`; adding a named CLI backend requires extending that composition code. Generic imports do not load optional SDKs or Linux-only runtimes.
