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
    contracts.py          Shared values and structural interfaces
    controller.py         Search lifecycle, scheduling, and artifact coordination
    workspace.py          Candidate reconstruction and canonical patch capture
    storage.py            Atomic records, checkpoints, and writer lock
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
        worker.py         SDK execution only
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
