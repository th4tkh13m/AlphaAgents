# Tree-search structure plan

## Goals

- Keep search, harness operation, editing agents, and validation independently replaceable.
- Give each coding-agent integration its own package and keep agent-specific workers, prompts, and telemetry there.
- Assign each artifact one owner and remove duplicate context and patch writes.
- Keep benchmark evaluation independent of optional engineering checks.

## Target layout

```text
tree_search/
  core/
    dgm/
      contracts.py        Candidate values and DGM bridge/mutator interfaces
      controller.py       Archive search lifecycle and scheduling
      workspace.py        Candidate reconstruction and canonical patch capture
      storage.py          DGM candidate records and JSON checkpoints
    mcts/                 Fixed-PUCT selection, artifacts, SQLite, and backup
    shared/
      storage.py          Atomic JSON and the common exclusive run lock
      workspace.py        Algorithm-independent Git commands
  infrastructure/
    process.py            Shared subprocess execution and evidence
    commands.py           Shared command-placeholder expansion
  bridges/
    command.py            Generic evaluator and resource adapter
    androidworld/         AndroidWorld resource and benchmark adapter
  mutators/
    command.py            Arbitrary external editing process
    coding_agents/
      codex/
        backend.py        MutationBackend adapter
        worker.py         Single DGM and native-role process entry point
        execution.py      Shared SDK Goal lifecycle and launcher helpers
        goal_objective.md External DGM Goal completion instruction
        roles/            MCTS role adapter, contracts, and Markdown prompts
        rollout_watch.py  Codex execution telemetry
        prompt.md         Codex instruction template
      # Add claude_code/ or another backend alongside codex/ when implemented.
  validators/
    changed_pytest.py      Optional checks for changed Python tests
  cli.py                  Composition and config resolution
  examples/
  tests/
```

The existing root Python API remains available through re-exports. Internal imports and docs move to the new paths. No empty or pretend Claude Code implementation is added: external command mutation supports invoking another coding agent immediately, while a future dedicated integration can implement the same MutationBackend interface.

## Ownership rules

| Concern | Owner |
| --- | --- |
| Harness instructions, protected paths, resources, benchmark score | Bridge |
| Editing execution, coding-agent prompt rendering, SDK telemetry | Mutator |
| Optional regression checks | Validator |
| Canonical mutation_context.json, validation records, patch, candidate status | Controller and core storage/workspace helpers |
| Process timeout, stdout/stderr capture, command expansion | Infrastructure |

The controller writes mutation context once. Command mutation consumes that file. Codex consumes its supplied context and writes its own SDK telemetry, but does not export the canonical patch. The controller captures partial edits after backend failures.

Validation is explicit and optional. AndroidWorld evaluation will not infer that every editing backend must produce live experiment summaries. Validation failures are diagnostic evidence and do not prevent independent benchmark evaluation of an otherwise permitted patch.

## Implementation order

1. Record this plan before edits.
2. Move core, infrastructure, and Codex files; update imports and package data.
3. Remove duplicate artifact writers and share command expansion.
4. Add validator contracts and optional validation composition; simplify AndroidWorld evaluation.
5. Update examples and documentation, including extension guidance.
6. Verify search behavior, backend swapping, validator independence, and package distribution.

## Completion evidence

- Regression suites pass with the new imports.
- Tests demonstrate benchmark evaluation with no validator and with failing optional validation.
- Tests prove canonical context is written once and patch capture works after editing failure.
- Generic imports do not load the Codex SDK or AndroidWorld runtime.
- An isolated wheel runs the offline example with its included resources.
- Documentation and examples use the actual final layout.

## Verification completed

Verified on 2026-09-30:

- Ubuntu/WSL: 72 tests passed, including AndroidWorld task continuation and episode cleanup.
- Windows: 43 tests passed; three AndroidWorld test modules skipped because the adapter requires Linux.
- Boundary tests cover single context serialization, provider consumption without patch export, optional validation failure with independent benchmark scoring, new-file patch capture, relative evidence patterns, and lazy generic imports.
- Existing search tests cover partial mutation failure, patch ancestry, scheduling, protected edits, archive policy, resource release, and resume.
- An extracted wheel ran from a separate directory with only the packaged code on PYTHONPATH: baseline score 0, child score 1, and optional missing evidence skipped. Codex prompt, AndroidWorld tasks, and the nested worker CLI were present and usable.
- Ruff lint and formatting checks passed; all Python source parsed with Python 3.10 syntax rules.

Real Codex sessions and live AndroidWorld services were not exercised; those require external credentials and runtime resources. A dedicated Claude Code integration remains a future sibling backend, with arbitrary editing commands already supported.

## Follow-up: no internal live-evidence requirement

Removed the live-evidence validator and mutation-owned experiment protocol. The coding-agent prompt requires implementation and proportionate checks; benchmark evaluation happens afterward through the bridge. The verification counts above describe the earlier refactor, before this follow-up.

## Fixed-PUCT extension: retain the established owners

The fixed-PUCT implementation follows these boundaries. Its staged location is
`core/mcts/`, with production composition in `tree_search/composition.py` and
dispatch through the existing CLI. The original DGM controller and root Python
API continue to use their existing contracts and behavior.

| Concern | Fixed-PUCT owner and reuse |
| --- | --- |
| Selection, trials, returns, immutable artifacts, SQLite state | `core/mcts/`; reuses `core.shared.storage.run_lock`, atomic JSON helpers, and `core.shared.workspace.git` |
| Plan, policy, implementation, judge, and value execution | `mutators/coding_agents/codex/roles/`; reuses the Codex rollout watcher and shared SDK serialization |
| Detailed role instructions and native Goal objective | Markdown assets in the Codex role package; supplemental static guidance is JSON |
| Screen/S0/Selection, device scheduling, task provenance | `bridges/androidworld/`; reuses the trusted Artemis runner and existing AndroidWorld runtime |
| Process execution and supervision | Existing `infrastructure/process.py` |
| Configuration and CLI | Existing `cli.py`, with provider composition in `composition.py` |

MCTS introduces its own search lifecycle and SQLite schema because frozen plan
actions, stochastic implementation children, predicted frontier returns, and
exactly-once backups differ from the DGM archive algorithm. Both algorithms
share the existing utilities; their persisted statistics are not interchangeable.
The core communicates through provider-neutral interfaces and imports neither
the Codex SDK nor the AndroidWorld adapter. Future role providers implement
those interfaces alongside the existing Codex integration.

`bootstrapped/` is removed. Both DGM and native role requests use the existing
Codex worker entry point and shared SDK execution primitives.
Historical run records remain inspectable. Execution resume must use matching
configuration, prompt assets, and evaluator identity; old unbound native request
packets cannot be replayed silently under a different prompt protocol.

The refactor is applied to the primary workspace. Protocol v13 retains exactly
three distinct plans per completed expansion and three implementation slots per
plan, filled through MCTS selection. Current verification passed 370
collected tree-search tests with no skips, including whole-coordinator SIGKILL
recovery immediately before and after backup. Changed-file lint also passed.
V13 adds an admitted diagnostic's pending receipt and resumes a blocked native
goal after that check finishes, preserving its thread, objective, accounting,
and original invocation deadline. The live v12 workers exposed premature
blocking while checks were still running; their frozen runtime and archived
failures remain unchanged. V13 also instructs implementers to audit test-created
outputs and reports exact offending paths when a patch exceeds scope. The
controller test proves such a patch produces no child or evaluation score.
Live v13 recovery acceptance remains pending.
The primary workspace now uses prompt protocol v14 for future runs. Its trusted
pytest runner detects the anonymous Unix socket send restriction that stalled
async test teardown and uses sendmsg for the same selector wakeup. The runner is
granted as one exact read-only file alongside the prepared verification venv;
role networking remains disabled. Verification passed 406 framework/bridge
tests and all 90 existing harness graph tests inside the installed Codex sandbox.
Evidence is in `runs/verification/async-verification-sandbox-20261011/`.
The running v13 experiments retain their frozen source and prompt identity;
their earlier rejected handoffs remain rejected. A fresh native Goal still needs
to demonstrate use of this runner, and live search/final Selection remain pending.
Historical v11 checks covered isolated wheel resource loading, the packaged
offline DGM example, native Codex capability/sandbox preflight, and an accepted
baseline judge with verified short file references. A separate v12 wheel
check verifies installed imports outside the source directory, byte-identical
external prompt resources and native Goal objective, all six role renderers,
and the four assessment schemas. Its evidence is retained under
`runs/verification/v12-package-20261010/`. The installed v12 wheel also completed
the packaged offline DGM example from outside the source directory: baseline 0,
child 1, no pending work. That experiment is retained under
`runs/experiments/alphaagents-v12-packaged-dgm-demo-20261010/`.
An isolated v13 wheel check also passed installed external resource loading,
all six role renderers and the four assessment schemas; its evidence is under
`runs/verification/v13-package-20261010/`.
The v12 one-worker baseline completed all Android tasks, but its final judge
cited a nonexistent source path and was rejected. A metered checkpoint resume
is retrying that judge with the completed Android evidence preserved. The v12 two-worker
run has a verified baseline score, three published native plans, and two
independently reserved attempts. Both native goals stopped while diagnostics
were pending and their handoffs were rejected; admitted checks are draining.
Its first trial completed with an invalid attempt and a transactional backup;
the second diagnostic is still draining. No child score is verified.

These checks establish software and package behavior. Complete one-worker and
two-worker search smoke runs, interrupted live-run recovery, the configured
experiment, and final Selection/export remain acceptance work. Exact commands,
identities, results, and limitations are recorded in `IMPLEMENTATION_AUDIT.md`
at the repository root. Historical verification counts describe their original
checkpoints rather than the current suite.
