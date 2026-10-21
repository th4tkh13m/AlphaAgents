# Harness-independent DGM search

DGM evolves a harness through code mutations and independent evaluations. The controller selects parents, schedules candidates, preserves lineage, and maintains scored, diagnostic, and retained archives. It has no Android, model SDK, task, or benchmark imports.

For a detailed explanation of parent selection, mutation, evaluation, archives, and scheduling, read [DGM tree search algorithm](DGM_ALGORITHM.md).

For the fixed-PUCT MCTS controller, staged AndroidWorld evaluation, and native
Codex roles, see [MCTS operation](core/mcts/README.md). Select it explicitly with
`controller.type="fixed_puct"`; existing DGM configurations retain their behavior.

## Structure

```text
tree_search/
  core/               DGM contracts, controller, workspaces, and persistence
    mcts/             Fixed-PUCT controller, transactional state, and artifacts
  infrastructure/     Shared process execution and command expansion
  bridges/
    command.py        Generic external evaluator and resource leases
    androidworld/     Mobile resources, benchmark execution, and recovery
  mutators/
    command.py        Arbitrary external editing command
    coding_agents/
      codex/          Existing mutation backend, SDK helpers, worker, and watcher
        roles/        Plan/Goal role adapter and external prompt assets
  validators/         Optional regression checks
  composition.py      Fixed-PUCT adapter composition
  cli.py              Shared commands and controller selection
  examples/           Offline harness and example configurations
  tests/              Search, boundary, backend, and recovery regressions
```

The actual harness source is supplied separately. It is copied into an immutable run snapshot; each candidate is reconstructed from that snapshot and its ancestor patches. Evaluator changes to a parent workspace do not become inherited source.

## Install and run

Requires Python 3.10+ and Git on PATH. From the `AlphaAgents` repository root:

Create new experiments under `runs/experiments/<experiment-name>/`. Keep the
configuration, logs, search state, artifacts, benchmark evidence, and plots in
that experiment directory. Use the same directory when resuming an experiment.

```bash
python -m pip install -e '.[test]'
python -m alpha_agents.tree_search --config alpha_agents/tree_search/examples/command.json --output runs/experiments/dgm-demo
```

The offline example uses Python 3 (`python3` in the example commands). It starts with a score of 0 and makes one deterministic mutation that scores 1. The mutation script demonstrates the interface; it is not a learning agent. Use a fresh output directory for each new run.

For Codex, install `.[codex,test]`, authenticate with `codex login`, and use a Codex mutation configuration. `requirements.txt` also installs the Codex runtime and test dependency directly.

Use `--stop-after-children 1` to run the baseline and one child, then exit after that child's mutation and evaluation lifecycle has settled. The configured total child budget remains unchanged. Continue the same archive with `--resume`; combine it with `--stop-after-children 1` to run one more child at a time, or omit the stop flag to finish the remaining budget. The stop limit also caps parallel launches, and a clean stop leaves no pending child work. Failed mutations count as finished child lifecycles and remain recorded for diagnosis.

Codex mutation threads use user-authorized full-access execution with approval policy `never` for unattended candidate tests and assigned-device ADB commands. The SDK calls this approval mode `deny_all`: permission requests are disabled, while full access lets commands run without requesting escalation. Both fresh and resumed threads use these settings, and execution telemetry records them. Mutator instructions still limit edits to disposable candidate code and keep original harnesses, benchmark definitions, scoring, and parent evidence immutable; these instructions are not a filesystem sandbox.

For AndroidWorld, copy and edit `examples/androidworld.json`. Supply the external mobile-agent checkout, interpreter and dependencies, model endpoint, model name, and AndroidWorld endpoint explicitly. AndroidWorld requires Linux/WSL; the general controller and command bridge also support Windows.

Paths in a CLI config are relative to that config file. Existing local command script paths are resolved before execution. Command arguments are passed as an argument list without a shell. `{workspace}`, `{artifacts}`, and `{resource}` are supported evaluator placeholders; mutation commands support `{workspace}`, `{artifacts}`, and `{context}`. Literal braces must be doubled.

## Automatic live profiling (fixed PUCT)

The production fixed-PUCT application automatically starts a Perfetto trace
watcher on `preflight()`, `run()`, or `finalize()`, including resumed runs. Normal
CLI runs need no additional command or Python package. The implementation lives
in `infrastructure/perfetto.py` (lifecycle and publication) and
`infrastructure/trace_export.py` (record-to-trace conversion). Composition owns
the watcher alongside the scheduler; search logic does not depend on profiling.
Use the application as a context manager or call `close()` to drain resources,
flush the final trace, stop the watcher, and close the state store.

Every five seconds, `<run>/profiling/live_trace.json` and
`live_trace.json.gz` are replaced atomically. Open either file at
<https://ui.perfetto.dev>. Uploaded traces are snapshots: reload the updated
file to see subsequent observations. `watcher_status.json` reports freshness,
export duration, file sizes, and failures; `resource_samples.jsonl` retains host
observations, and `snapshots/` preserves compressed minute snapshots. One
exclusive observer lock prevents duplicate exporters. Observation errors are
logged and retried without interrupting the experiment.

Tracks show device task assignment, idle time, quarantine and recovery; task
execution and recorded subphases; device and implementor queues; Codex lifecycle
and native worker time; separate baseline and child assessment phases; and
evaluation stage envelopes. Stage envelopes include recorded dispatch queue
time and retries. Queue and task slices explain those envelopes and must not
be added to them. Code review can overlap Android work, and different devices
can execute concurrently; sum task time to measure device consumption, and use
wall-time envelopes or interval unions to measure latency. A device task
assignment covers its scheduler callback, which can include setup/reset and
cleanup; it does not assert continuous agent or model activity.

CPU (percent of one host core) and RSS are sampled for emulators with matching
PID/birth records in `devices/status.json`; absent records mean unavailable
resource measurements, not zero usage. Device assignment tracks still work
using the configured serials and recorded dispatches. Host memory availability
and pressure are whole-machine observations. Resource sampling begins when the
watcher starts; historical CPU data is not inferred. These traces contain
logical application spans, not kernel scheduling, GPU activity, or call stacks.
Ongoing spans are clipped to the observation time and marked incomplete.
Task subphases use a current same-host monotonic/realtime clock anchor; completed
native worker endpoints reconstructed from metadata modification time are
explicitly approximate. Native elapsed measurements remain available in role
metrics. No benchmark database, harness, score, or experiment identity is changed.

To attach the same packaged watcher to an already-running frozen runtime, or
export a one-time snapshot:

```bash
python -m alpha_agents.tree_search.infrastructure.perfetto --run runs/experiments/NAME --watch
python -m alpha_agents.tree_search.infrastructure.perfetto --run runs/experiments/NAME
```

For an attached watcher, `--owner-record PATH` accepts a live process JSON record
with `pid` and `process_birth` and stops the watcher when that exact process exits.
Automatic application-owned watchers stop with `SearchApplication.close()`.
The exporter uses Python's standard library and the Chrome trace JSON format
supported by Perfetto. The official `perfetto` Python package is optional for
trace building or SQL analysis; it is not needed to run this watcher.

## Bridge API

Implement `HarnessBridge` in `core/dgm/contracts.py` and construct `DGMController(bridge, mutator, output, config, validators=())` directly:

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
    Path("runs/experiments/dgm-example"),
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

Search implementations have separate homes: [DGM](core/dgm/README.md) in
`core/dgm/`, and [fixed-PUCT MCTS](core/mcts/README.md) in `core/mcts/`.
`core/shared/` holds only their common atomic JSON, run-lock, and Git helpers.
The public Python imports remain available from `alpha_agents.tree_search`;
internal DGM imports now use `alpha_agents.tree_search.core.dgm`.

Fixed-PUCT integrations implement the explicit agent and evaluator contracts in
`core/mcts/ports.py`. Core owns search decisions and state; agent adapters own
model execution and prompts; evaluation bridges own benchmark/runtime recovery.
`composition.build()` returns a context-managed `SearchApplication` that owns
external resources and closes the scheduler before search state. See the
[MCTS integration boundaries](core/mcts/README.md#integration-boundaries-and-motivation)
for replacement adapters, lifecycle ownership, and frozen-identity compatibility.

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

### Supervised task timeout handoff

A singleton task keeps the 2,100-second device envelope: 120 seconds for startup, 1,800 for task execution, and 180 for cleanup. The trusted runner reserves one second inside the task envelope for publishing the cleanup phase (1,799 seconds of agent execution). When execution expires, it enters cleanup before awaiting cancellation; recording and trace finalization use the existing cleanup allowance. Completed timeout cleanup yields the existing trusted agent-failure outcome with zero success and no invented AndroidWorld grader reward. Stuck cancellation, blocked event loops, or unconfirmed cleanup remain infrastructure failures. The receipt records the handoff reserve, and runner source hashing separates this behavior from frozen experiments.

### Private Codex inspection scratch

Prompt library v18 gives each invocation its own writable `role_requests/<request_id>/scratch` folder and publishes that exact path in the role envelope. Assessment and planning roles retain read-only source/evidence access; scratch writes support safe SQLite DB/WAL/SHM copies, standalone backups and other derived inspection files. The scratch grant excludes the surrounding request directory and other invocations. Scratch output is not canonical evidence and cannot be cited as a task sample. Request IDs cannot traverse paths, and scratch-directory symlinks are rejected. Real sandbox probes verified source-write denial, assigned scratch writes and WAL-backed inspection with unchanged original hashes. Frozen older runs keep their original profiles and versions.

The v29 fixed-PUCT runtime shares read-only artifact views and sealed SQLite
inspection snapshots, and retires checkpointed temporary workspaces. See the
[storage lifecycle](core/mcts/README.md#storage-lifecycle-v29) for recovery and
evidence-retention guarantees. Existing runs are not retroactively cleaned.


For the complete 25-task development curriculum, use
[`examples/all_development_sequence.json`](examples/all_development_sequence.json).
It evaluates the carried harness afresh on all Screen and Selection tasks,
ordered by difficulty (easy, medium, hard), optimal human steps, then task name.
Passing roots skip MCTS; failed roots use three plans, two implementation slots
per plan, depth four, and up to thirty rollouts. A task's selected exact artifact
becomes the next root. Confirmation tasks and historical pass/fail labels do not
select the curriculum or skip new measurements. `task_order.json` freezes the
ordering metadata; altered metadata is rejected on rebuild. Existing cached-failure
sequences remain available through `sequence.task_scope=cached_failures`.
