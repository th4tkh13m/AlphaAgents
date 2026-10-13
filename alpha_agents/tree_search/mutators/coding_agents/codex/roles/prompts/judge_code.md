
## Phase: code review during independent evaluation
Inspect control flow, prompts/tools, state and memory handling, error paths,
retry bounds, verification, and potential shortcuts in the exact artifact.
Canonical evaluation proceeds independently while you work. This phase produces
preliminary findings and may produce provisional dimension ratings; it cannot
publish canonical J or admit an eligible child. Identify specific mechanisms
and trajectory questions for the final phase. Do not wait for unavailable
canonical results, treat code plausibility as measured behavior, or overwrite a
previous artifact's assessment. Completion is a developed code review with
trace-grounding questions, evidence references, uncertainties, and clear limits.

## Assessment rubric: code-review-v1
Inventory relevant entry points, state transitions, interfaces, dependencies and
error/completion paths from the actual supplied source. For each common quality
dimension, record a source location, the mechanism it establishes, an adverse or
boundary condition, a plausible counterexample and the evidence still needed.
Trace central behavior end to end; do not infer coverage from a directory listing
or reward naming, comments, abstraction count or patch size. Distinguish an
observed source defect, a contingent risk and a plausible but unverified mechanism.

Completion requires developed findings across all three common dimensions,
explicit inspected/unseen coverage and concrete execution questions. Provisional
ratings use the common anchors only as source-based hypotheses, with their
limitations stated. They cannot publish canonical J or manufacture execution
observations. Partial findings remain useful input to the independent final phase.

Set phase=`code`; clearly label preliminary conclusions. If time runs out,
preserve partial findings. The execution reviewer can proceed with explicit
knowledge that this phase was partial/unavailable.
