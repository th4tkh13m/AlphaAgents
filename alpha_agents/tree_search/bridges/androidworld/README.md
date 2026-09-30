# AndroidWorld bridge

`bridge.py` implements the generic harness bridge. `runtime.py` handles HTTP probes, device episode cleanup, mobile evidence. `evaluation.py` checkpoints tasks, retries runtime failures, and supervises evaluator processes. `tasks.json` is the fixed task manifest inherited from harness-optimization.

The harness is the external Mobile-Agent-v3.5 checkout, not this adapter. Its task runner remains `scripts/run_ma35_on_docker.py`. Install that checkout's dependencies in its own environment and configure `python` and `venv_source` accordingly.

Set `androidworld_api_url`, `base_url`, and `model` explicitly. `androidworld_devices` is a list of device IDs; an empty list uses one exclusive legacy endpoint. The lease holds a device for the whole candidate lifecycle, including any mutation-owned experiments.

`prepare()` supplies AndroidWorld-specific editing guidance and runtime constraints through `MutationContext.instructions`. Coding-agent templates belong to their mutator packages; this bridge has no replacement mutation prompt. The example uses the default Codex template, which incorporates these supplied instructions.

All candidates in a run use the same `score_stage`, defaulting to `evaluation`. The bridge does not require the editing backend to perform live experiments. Configure independent validators when focused regression tests are desired. Validator failures remain diagnostics and do not prevent benchmark scoring. Transport failures and missing/incomplete task evidence carry no score.

Evaluator subprocesses run through the package module entrypoint, with the package root propagated to their environment. Evaluation checkpoints and episode cleanup artifacts are preserved under each candidate. Task recovery behavior is covered by the continuation and cleanup regression tests.

The mutating agent performs test cases and a representative harness test run using the assigned resource. No validator checks for saved experiment-summary files. Independent benchmark evaluation and its artifacts remain owned by the bridge.
