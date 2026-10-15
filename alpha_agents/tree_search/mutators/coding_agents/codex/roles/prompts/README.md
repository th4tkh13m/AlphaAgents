# Codex role prompts

Edit these Markdown files to change the framework instructions. Python loads their exact UTF-8 contents. Each request combines `shared.md`, its role template, and the serialized invocation inputs. The two judge phases also prepend `judge_common.md`. Native Goal objectives load `goal_objective.md` and fill only the exact plan, attempt, artifact, and context-path placeholders.

`proposer.md` specifies native Plan and archive diversity; `implementer.md` specifies native Goal and autonomous implementation. The other templates define policy, value, and independent harness review. Keep prompts detailed and preserve their evidence, autonomy, and completion requirements.

Prompt contents contribute to the persisted library and rendered-request hashes. Content changes require compatible versioning or a fresh run; do not edit an active experiment’s prompts.

`context_guidance.json` contains static reference, trajectory, utility,
verification, test-scope, and novelty guidance. The adapter combines it with
supplied machine data; core planning contracts retain only versions and
admissible metadata values, without model-facing instruction paragraphs.
The v22 library identity covers these values and `goal_objective.md`, as well
as every main role template. Native request packets carry the exact expected
library and rendered-prompt hashes; worker loading rejects changed assets.

## Domain-neutral rubrics

Static instructions do not name an application, platform, benchmark, model,
source layout or task family. The artifact, evaluator contracts, file catalog
and permitted invocation context supply those semantics. Example source paths
illustrate reference syntax only; they are not evidence and do not establish
that a file exists. Optional diagnostic/storage instructions apply only when
the adapter supplies that capability or evidence format.

| Role | Rubric and output |
| --- | --- |
| Proposer | `proposer-v1`: supported diagnosis, executable intervention, feasible edit/test scope, mechanism diversity, falsifiable checks, and discounted allocation relevance; developed plans, not invented numerical scores |
| Policy | `policy-v1`: causal support, timing, feasible continuation, downside/uncertainty and marginal information; relative allocation priors, not success probabilities |
| Implementer | `implementer-v1`: fidelity, correctness, integrity, actual verification and auditable handoff; completed/partial/failed engineering status, not artifact-quality scores |
| Code judge | `code-review-v1`: source coverage, mechanisms, counterexamples and execution questions; explicitly preliminary findings |
| Execution judge | `execution-review-v1`: identity-bound observations, source/trace reconciliation, alternative causes, contradiction and coverage review; one final independent quality assessment |
| Value | `value-v1`: baseline binding, mechanism support, continuation scenarios, discounted timing, uncertainty and contract audit; expected discounted net improvement |

Both judge phases share `artifact-quality-rubric-v2` in `judge_common.md`.
It defines fifteen behavioral anchors: five scores for each of robustness,
decisions and generalizable. Confidence describes evidence support and does
not rescale a rating. Supported mixed quality is distinct from an unknown
dimension; unknowns retain the existing explicit-null and neutral aggregation
convention. Earlier code findings are hypotheses, not additional score votes.
Incoming plans, parent comparisons and implementation effort remain excluded.

Plan repair reuses proposer gates and adds `plan-repair-v1` for whole-document
correction without losing investigated substance. Rubrics use existing narrative,
coverage, status and evidence fields; they do not add output-schema dimensions or
change utility weights. The production objective binds the quality-rubric version,
and all rubric text is included in prompt/library hashes. New instructions require
a fresh compatible experiment identity rather than relabeling historical ratings.

`alpha_agents/tree_search/tests/test_role_rubrics.py` checks rendered integration for all six roles,
domain neutrality, all fifteen quality anchors, unknown aggregation, supplied
semantics preservation and rubric/prompt identity changes. Software checks
establish the contract, not empirical agreement or calibration of live graders.
