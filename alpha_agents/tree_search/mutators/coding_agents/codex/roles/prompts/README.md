# Codex role prompts

Edit these Markdown files to change the framework instructions. Python loads their exact UTF-8 contents. Each request combines `shared.md`, `scratch.md`, its role template, and the serialized invocation inputs. Single-task requests also append `single_task.md`; later proposer calls append existing-plan instructions. The active `judge_review` role loads `judge.md`. Native Goal objectives load `proposer_goal_objective.md` for the read-only proposer and `implementer_goal_objective.md` for the implementer, binding each objective to its exact invocation and context file.

`proposer.md` specifies one technical design document in a read-only native Goal; `implementer.md` specifies autonomous implementation in a writable native Goal. Both use default collaboration mode. The proposer returns full Markdown with a JSON summary, while the implementer returns JSON. The other templates define policy, value, and independent harness review. Keep prompts detailed and preserve their evidence, autonomy, and completion requirements.

Prompt contents contribute to the persisted library and rendered-request hashes. Content changes require compatible versioning or a fresh run; do not edit an active experiment’s prompts.

`context_guidance.json` contains static reference, trajectory, utility,
verification, test-scope, and novelty guidance. The adapter combines it with
supplied machine data; core planning contracts retain only versions and
admissible metadata values, without model-facing instruction paragraphs.
The v34 library identity covers these values and both role-specific Goal objectives, as well
as every main role template. Native request packets carry the exact expected
library and rendered-prompt hashes; worker loading rejects changed assets.

## Structured value forecast prompt

`value.md` defines the revised `value-v2` forecast in ten sections. Its six rubric
assessments cover opportunity, reachability, implementation/outcome uncertainty,
utility impact, timing/persistence, and forecast uncertainty. The JSON report
includes baseline U/J/F, evidence-labelled claims, scenario probabilities,
transition trajectories, explicitly discounted remaining continuation,
scenario contributions, sensitivity, and inspection coverage. It preserves the
continuing-policy target and signed value bounds, including the supplied
single-task success rule as an absorbing state when enabled. Evidence labels and
confidence do not multiply the return.

The active `value` role loads `value.md`. Core `value_forecast.py` defines the
closed `value-v2` schema and validates baseline binding, all six rubric records,
scenario probabilities, transition indexing, U/J/F arithmetic, discounted tails,
scenario contributions, final value, sensitivity, and bounds. The invocation
supplies an absolute arithmetic tolerance of 1e-9; this checks reported numerical
consistency and does not establish empirical calibration of subjective estimates.

Curated ancestry is frozen into each frontier snapshot and supplied as
`prior_implementations`, including the current artifact's implementation and
excluding the root. Recovery reuses that frozen history and evidence cutoff.
Reports retain individual claims, scenarios, and coverage in authoritative value
provenance and status/calibration reporting. Invalid or incomplete assessments
keep the trial pending without a synthetic zero or backup. The current runtime
dispatches only the structured `value-v2` prompt in `value.md`.
Prompt version v34 and continuing-protocol version v6 require a compatible fresh
experiment identity; historical forecasts are not relabeled as value-v2.

## Unified judge prompt

`judge.md` is the new integrated engineering-review prompt. It specifies one
review after canonical evaluation, developed findings before ratings, and forty
criteria across correctness, reliability, maintainability, and generalizability
and evaluation integrity. Each criterion receives an integer rating from 0 to 4
or an explicit unknown/non-applicable assessment. The JSON report keeps integrity
findings visible independently of criterion ratings and leaves aggregation to
the controller.

The active controller makes one `judge_review` call after canonical S0 evidence
is available. `artifact-quality-rubric-v3` binds the closed forty-criterion report.
The controller computes raw J as the sum of rated criterion scores divided by
four times their count; every rated criterion has equal weight. Unknown and
non-applicable criteria are excluded without substituted points and remain
visible in reports. No rated criteria means unavailable. Confirmed evaluation
manipulation forces effective J to zero independently of the raw aggregate.

Root and child reservations, recovery, score publication, and final reports use
this protocol. Rubric and prompt/library identities change for new experiments;
historical snapshots retain their original scoring semantics. The former
code/execution prompt files and role dispatch have been removed.

## Domain-neutral rubrics currently loaded by the runtime

Static instructions do not name an application, platform, benchmark, model,
source layout or task family. The artifact, evaluator contracts, file catalog
and permitted invocation context supply those semantics. Example source paths
illustrate reference syntax only; they are not evidence and do not establish
that a file exists. Optional diagnostic/storage instructions apply only when
the adapter supplies that capability or evidence format.

| Role | Rubric and output |
| --- | --- |
| Proposer | One developed seven-section technical design document with a JSON summary, grounded diagnosis, ancestry implementation history, concrete mechanism, implementation steps, validation, and risks; later calls must not overlap existing node-local plans |
| Policy | `policy-v1`: causal support, timing, feasible continuation, downside/uncertainty and marginal information; relative allocation priors, not success probabilities |
| Implementer | `implementer-v1`: fidelity, correctness, integrity, actual verification and auditable handoff; completed/partial/failed engineering status, not artifact-quality scores |
| Unified judge | `artifact-quality-rubric-v3`: integrated source/execution review, developed findings, forty criterion ratings, integrity conclusion, and explicit coverage; controller-owned normalized score |
| Value | `value-v2` in `value.md`: six developed rubric assessments, scenario probabilities and trajectories, discounted calculations, sensitivity, and structured JSON |

Plan repair resumes the same persisted proposer Goal with validation feedback
in its context file. It returns the complete corrected document and summary,
preserving the original objective, deadline, identities, and investigated substance. The unified judge adds its own criterion/report schema; combined utility weights
remain controller-owned. The production objective binds the quality-rubric version,
and all rubric text is included in prompt/library hashes. New instructions require
a fresh compatible experiment identity rather than relabeling historical ratings.

`alpha_agents/tree_search/tests/test_role_rubrics.py` checks rendered integration,
domain neutrality, supplied semantics, and prompt identity.
`alpha_agents/tree_search/tests/test_unified_judge.py` checks the forty-criterion
schema, normalization, unknown/non-applicable states, integrity consistency,
evidence boundaries, and v3 root/child score publication. Recovery and final
report integration are also exercised by the controller/coordinator tests.
Software checks establish these contracts, not empirical reviewer calibration.
