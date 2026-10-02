# AndroidWorld bridge

`bridge.py` implements the generic harness bridge. `runtime.py` handles HTTP probes, device episode cleanup, mobile evidence. `evaluation.py` checkpoints tasks, retries runtime failures, and supervises evaluator processes. `tasks.json` is the fixed task manifest inherited from harness-optimization.

The harness is the external Mobile-Agent-v3.5 checkout, not this adapter. Its task runner remains `scripts/run_ma35_on_docker.py`. Install that checkout's dependencies in its own environment and configure `python` and `venv_source` accordingly.

Set `androidworld_api_url`, `base_url`, and `model` explicitly. `androidworld_devices` is a list of device IDs; an empty list uses one exclusive legacy endpoint. The lease holds a device for the whole candidate lifecycle, including any mutation-owned experiments.

`prepare()` supplies AndroidWorld-specific editing guidance and runtime constraints through `MutationContext.instructions`. Coding-agent templates belong to their mutator packages; this bridge has no replacement mutation prompt. The example uses the default Codex template, which incorporates these supplied instructions.

All candidates in a run use the same `score_stage`, defaulting to `evaluation`. The bridge does not require the editing backend to perform live experiments. Configure independent validators when focused regression tests are desired. Validator failures remain diagnostics and do not prevent benchmark scoring. Transport failures and missing/incomplete task evidence carry no score.

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
- `adb_path`, `console_port`, `grpc_port`: the installed ADB executable and running emulator ports.
- `androidworld_devices`: exactly one ADB serial, such as `emulator-5560`.
- `base_url` and `model`: the model endpoint and its advertised model ID.
- `task_file`, `score_stage`, `seed`, `task_timeout`, and `stage_timeout`: the fixed evaluation contract.

Use absolute paths in this configuration. `llm_timeout` configures supported
Artemis role timeouts and the stream chunk timeout; the outer process timeout
kills the evaluator process group. The original harness decides its remaining
LLM behavior. Task rewards come from the trusted AndroidWorld checkout.

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
  alpha_agents/tree_search/bridges/androidworld/test_artemis.py
```
