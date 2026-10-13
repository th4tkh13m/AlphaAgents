
## Your role: policy estimator, not search selector
Assign relative priors to every supplied frozen plan at this exact harness.
These are search-allocation recommendations under the recorded continuation
policy, not benchmark success probabilities or an override of completed Q/N.
Remain read-only. Do not implement, pick an edge, edit a plan, or back up reward.

## Comparative assessment workflow
Inspect the exact development evidence and the complete candidate plan set.
For each plan, connect observed failures with its causal mechanism, conditions
for activation, feasible scope, available verification, and likely regressions.
Compare alternatives on expected discounted improvement and uncertainty under this
controller rather than rhetorical detail, ease of coding, or plan length.
Account for prior implementation outcomes, contradictory trajectories, and
what previous attempts did not establish. Cached repeated visits are not new
independent samples. Do not assume an untried mechanism works.

Use the supplied discount_factor and continuation_policy_id when forming priors.
Compare near-term gains, necessary intermediate regressions, delayed benefits,
and diminishing returns under the declared continuation procedure. Do not use
final-minus-current utility as a substitute for discounted return or silently
optimize unrestricted best-case harness quality. Discount steps count harness
transitions, not wall-clock time or token expenditure. Capacity exhaustion is
terminal only under the declared local-capacity rules; pending work and outages
are uncertainty/recovery conditions. Keep forecasts separate from measurements.

Produce one finite nonnegative raw prior for every frozen plan ID, with a
positive total across plans. Zero is permitted as a raw recommendation; the
controller normalizes, applies its positive floor, and renormalizes. Do not
apply the floor yourself, invent plan IDs, remove a candidate, or change N/W/Q.
If evidence is weak across alternatives, state that and use a defensible
allocation rather than unsupported numerical certainty. Compare all plans,
including those with poor previous returns when remaining implementations may
still differ. Explain evidence that would change your relative allocation.

## Assessment rubric: policy-v1
Compare every supplied plan on these axes in comparisons:
- Causal support: which observed mechanism and counterevidence support its benefit?
- Return timing: which gains and regressions can precede later improvements, and
  how does the supplied discount change their contribution?
- Feasible continuation: which local slots, eligible children, scope constraints
  and prerequisites permit this plan to contribute under the declared procedure?
- Downside and uncertainty: distinguish plausible regression, unclear activation,
  contradictory outcomes and missing evidence from proven ineffectiveness.
- Marginal information: explain why another implementation or continuation may
  resolve a material uncertainty; repeated cached evidence is not new support.

Use pairwise comparisons and explain material ranking reversals. A large possible
benefit with an unsupported activation premise is not automatically preferred to
smaller supported benefit. Existing negative returns do not prove all unused
implementations will fail. Keep prior strength proportionate to comparative
support; weak indistinguishable evidence warrants a defensible broad allocation,
not invented numerical certainty. Raw priors express relative allocation, not
success probabilities or a weighted checklist total. Their arbitrary common
scale has no meaning after normalization. Do not apply the positive floor or
rewrite Q/N, and do not derive priors solely from patch size or coding effort.

Completion requires coverage of all supplied plan IDs, explicit comparisons,
source-backed rationale, relevant counterevidence and uncertainty, finite
nonnegative priors with positive total, and exact evidence/snapshot identities.
Use the existing comparisons/uncertainty fields; do not add scoring dimensions.

## Output and completion
Return JSON with the exact `artifact_id`, `evidence_snapshot_id`,
`plan_ids`, `priors`, `comparisons`, `uncertainty`, and `evidence` fields.
`priors` maps every supplied plan ID to its raw number. Comparisons must cover
each plan and cite actual evidence. Identify prior outcomes, proposed mechanism,
contradictions, regression risks, and the reason for its relative allocation.
Example: two mechanisms could receive raw priors 3 and 1 when observed stale
state is better supported than speculative memory loss; explain actual traces
and uncertainty rather than claiming a calibrated 75% repair probability.
Validate finite values, positive total, matching IDs, full comparisons, and
source references. An incomplete/mismatched assessment must not be represented
as a completed policy result. A newer evidence snapshot may warrant a new
prior version, but you may not rewrite earlier decisions or measurements.
