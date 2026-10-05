# AndroidWorld bridge

`bridge.py` implements the generic harness bridge. `runtime.py` handles HTTP probes, device episode cleanup, mobile evidence. `evaluation.py` checkpoints tasks, retries runtime failures, and supervises evaluator processes. `tasks.json` contains the frozen template sets derived from `androidworld_sets_manifest.json`: 5 Screen, 20 additional Selection, and 91 held-out Confirmation tasks. All three sets are disjoint.

The harness is the external Mobile-Agent-v3.5 checkout, not this adapter. Its task runner remains `scripts/run_ma35_on_docker.py`. Install that checkout's dependencies in its own environment and configure `python` and `venv_source` accordingly.

Set `androidworld_api_url`, `base_url`, and `model` explicitly. `androidworld_devices` is a list of device IDs; an empty list uses one exclusive legacy endpoint. The lease holds a device for the whole candidate lifecycle, including any mutation-owned experiments.

`prepare()` supplies AndroidWorld-specific editing guidance and runtime constraints through `MutationContext.instructions`. Coding-agent templates belong to their mutator packages; this bridge has no replacement mutation prompt. The example uses the default Codex template, which incorporates these supplied instructions.

All candidates in a run use the same `score_stage`, defaulting to `selection`. Children first run the five Screen tasks when the manifest includes that set. All Screen tasks must pass before Selection runs; a failed gate retains Screen diagnostics and has no ranking score. Screen results never contribute to the Selection score. Use `screen` for the five-task execution set. `confirmation` evaluates held-out tasks with an explicitly supplied fresh `seed`; the bridge refuses to prepare mutations against that set. The old `evaluation` stage name aliases Selection for the new sets, while explicit legacy manifests remain supported. Configure independent validators when focused regression tests are desired. Validator failures remain diagnostics and do not prevent benchmark scoring. Transport failures and missing/incomplete task evidence carry no score.

For local Artemis, enable `full_benchmark: true`, set an explicit persistent `benchmark_cache_dir` outside candidate directories, and choose a `confirmation_seed` different from the Selection seed. The root runs all 116 tasks before mutation, and the highest Selection-scoring candidate receives a full audit after the child budget completes. Full results and trajectories remain in the separate cache; only Selection evidence is copied to parent directories. Required audit infrastructure failures stop the controller rather than declaring completion. Full reports include both strict pass rate and mean reward, retaining partial credit.

Completed cache entries are matched by agent files, evaluator and bridge files, installed Python packages, task sets, model/endpoint, seeds, and runtime settings. Supply `model_revision` and `fixture_identity` when model weights or device fixtures change under unchanged names. Reuse rechecks the saved evaluator process and per-task manifests; incomplete or invalid stages are retained separately and rerun. Reports at the search root link to the persistent audit. Confirmation results must never guide mutations.

Set `evaluation_workers: 2` and configure two `androidworld_devices` with distinct console/gRPC port mappings to evaluate disjoint task batches concurrently. Each runner uses separate output and temporary directories; task parameter seeds are stable across sharding. Mutation still uses a single leased device. Evaluations borrow only idle devices without blocking on another lease, so concurrent searches fall back to fewer evaluation workers safely. Child scores still come exclusively from the 20 Selection tasks.

`task_file` accepts the original row-based manifest or a flat stage-to-task mapping. Historical `artemis_success`, runtimes, and seeds are not used as candidate scores or newly frozen parameter settings. Paired Selection instances use the same configured seed for every candidate. Configure Confirmation seeds separately. Audit the answer/evaluator integration for information-retrieval tasks and preflight VLC setup as specified in the manifest notes.

Evaluator subprocesses run through the package module entrypoint, with the package root propagated to their environment. Evaluation checkpoints and episode cleanup artifacts are preserved under each candidate. Task recovery behavior is covered by the continuation and cleanup regression tests.

The mutating agent performs test cases and a representative harness test run using the assigned resource. No validator checks for saved experiment-summary files. Independent benchmark evaluation and its artifacts remain owned by the bridge.

## Artemis with a local AndroidWorld emulator

Set `evaluation_runner` to `artemis_local` to evaluate Artemis through its public
`Agent` and `Builders` APIs. `artemis_runner.py` is a trusted bridge-owned runner
outside the editable candidate. Both the original Artemis and Mobile-Agent
directories stay unchanged; DGM mutations apply to disposable candidate copies.
No AndroidWorld HTTP gateway or runner inside the harness is required.

The core excludes `output*` when copying source. Artemis has required modules
and prompts with those names, so this bridge builds a separate source snapshot
with tracked backing files in `.dgm_preserved_modules`. Candidate preparation
exposes ignored symlinks at the original paths. The mapping tells the mutator
which backing files to edit; those edits are captured in ordinary child patches.
Neither the core copying rules nor the original harness files are changed.

In the AndroidWorld bridge configuration, supply:

- `python` and `venv_source`: a Python 3.12 environment containing Artemis and AndroidWorld dependencies.
- `androidworld_root`: the trusted AndroidWorld checkout, outside the candidate.
- `adb_path`: the installed ADB executable.
- `androidworld_devices`: one or more ADB serials, such as `emulator-5560`.
- `androidworld_device_ports`: a mapping from each serial to its `console_port` and `grpc_port`. Single-device configurations can still use top-level `console_port` and `grpc_port`.
- `base_url` and `model`: the model endpoint and its advertised model ID.
- `task_file`, `score_stage`, `seed`, `task_timeout`, and `stage_timeout`: the fixed evaluation contract.

Use absolute paths in this configuration. `llm_timeout` configures supported
Artemis role timeouts, the stream chunk timeout, and the default hard limit in
its shared model-call invocation helper. The runner configures that helper in
memory; original harness files remain unchanged, and explicit per-call limits
remain respected. The outer process timeout kills the evaluator process group.
Task rewards come from the trusted AndroidWorld checkout.

Each candidate saves `stages/<stage>/evaluation_request.json`, `manifest.json`,
`summary.json`, `runner/process.json`, stdout/stderr, checkpoints, and Artemis
traces. The controller receives a normalized score plus individual task results
and errors. Incomplete, duplicate, mismatched, timed-out, or exception-bearing
results are diagnostics with no score, except for explicitly identified agent
execution timeouts and graph transition limits. Those bounded agent failures
receive a task score of zero and retain the candidate's aggregate score. Their
exceptions remain in the task results; `androidworld_reward` is null because
the grader did not run. The trusted runner records the failure phase and kind.
Setup, answer-submission, grading, cleanup, and evaluator failures remain
invalid evidence. Historical manifests without that classification are not
silently reinterpreted. A valid completed task with reward zero is also a scored
failure, distinct from an evaluator failure.

The mutator receives the parent evaluation, copied `.dgm_parent_evidence`, the
assigned serial and ports, the model endpoint, and a representative run command
that imports its candidate copy. An ignored `venv` symlink exposes dependencies.
The benchmark checkout and bridge runner remain outside its editable workspace.

Run bridge contract and repository regression tests with:

```bash
python -m pytest -q alpha_agents/tree_search/tests \
  alpha_agents/tree_search/bridges/androidworld/test_artemis.py \
  alpha_agents/tree_search/bridges/androidworld/test_task_sets.py \
  alpha_agents/tree_search/bridges/androidworld/test_parallel.py
```

## Four-device tree search

Use [artemis_parallel.json](artemis_parallel.json) as a configuration template.
Replace its source, dependency environment, AndroidWorld, ADB, and model values.
Start four separate, configured AVDs at the listed console/gRPC port pairs;
each emulator must have its own writable AVD data. The bridge attaches to running
emulators rather than starting them.

Set `search.workers` to 4. Asynchronous scheduling admits each completed child
and fills its slot immediately. Synchronous scheduling also supports four
workers when `batch_size` is 4, but waits for the whole batch before choosing
more parents. The initial baseline is evaluated before child search starts.

Each child leases one device for its entire mutation, representative experiment,
and independent evaluation. Its mutator receives only that device's serial and
ports. Leases are returned on completion or error; additional workers wait when
all devices are busy. Artemis uses its public per-device concurrency mode, with
separate traces, databases, checkpoints, and candidate workspaces. Port validation
rejects console/ADB/gRPC collisions and serial mismatches.

Parallelism is across candidates: each candidate still runs the same full
Selection set and paired seeds on its assigned device. Tasks within a candidate
remain sequential. Four workers share the model endpoint, so emulator count
alone does not determine throughput. This configuration is not a hardware limit.

The local OpenAI-compatible runner defaults to `openai_memory_compatibility: true`.
The preserved harness's visual step summarizer and history capsule compressor
directly construct Google clients rather than using the configured LLM roles.
The bridge disables those two optional services through public configuration
flags, keeps transcript history configured as supplied by the candidate, and
writes the effective configuration to `runtime_agent_config.json`. The manifest
records the resulting memory flags. Every candidate in a run uses the same
compatibility setting; this is a configured OpenAI-backed agent evaluation.
Set the option to false only when the environment supports the preserved
Google-specific memory paths.

Set `perform_emulator_setup: true` to run AndroidWorld's official app installation
and configuration before each evaluation or mutator experiment. It uses the
selected emulator and may reset benchmark app data. Even when setup is disabled,
the local runner checks installed packages for every concrete selected task
instance before running the agent, including app names assigned dynamically by
information-retrieval tasks. Missing apps invalidate the runtime rather than
being counted as agent failures. The manifest records setup and preflight results.

`fixture_preflight` defaults to true. Before running the agent, the trusted bridge
initializes and tears down fresh copies of every selected task fixture, using the
same parameters, without calling the model or collecting rewards. This catches
incomplete onboarding, missing database tables, and fixture cleanup errors that
package installation checks cannot detect. The manifest records each result;
any failure prevents agent evaluation. Repair device onboarding and save its
official app snapshot before retrying. Use `perform_emulator_setup: false` for an
already prepared device to preserve manually repaired snapshots.

For information-retrieval tasks, the runner requests an `answer` string through
Artemis's public structured-output API and submits that exact string through
AndroidWorld's public answer action before grading. The episode records
`agent_answer` and `answer_submitted`. Missing output or a failed handoff invalidates
the runtime; an incorrect submitted answer remains a measured task failure. The
bridge never derives an answer from benchmark success criteria.
