## 1. Purpose and design status

AlphaAgents searches for improved mobile-GUI agent harnesses through repeated diagnosis, plan generation, implementation, and evaluation. The harness includes executable code, prompts, configuration, and other assets that determine behavior. The underlying GUI-agent model and benchmark execution conditions must be recorded so improvements can be attributed to the harness.

The system preserves exact generated artifacts and searches a persistent tree of modifications. It uses measured utility changes as rewards and Codex predictions of future utility change to bootstrap frontier returns. A PUCT controller combines these backed-up returns with Codex-generated plan priors.

This note consolidates the current design for engineering implementation and research review. **Agreed choices** are distinguished from **proposed implementation details** and **open decisions**. It is a design specification, not a claim of demonstrated performance.

Source discussion: Implementation Idea.

### Agreed initial configuration

| Item | Initial choice |
| --- | --- |
| Artifact state | Exact harness |
| Modification action | Frozen `PLAN.md` contents |
| Implementation | Stochastic Codex transition |
| Plan proposal and priors | Codex action proposer and policy estimator |
| Selection in the initial controller | PUCT |
| Reward | Child frozen combined utility F minus parent F |
| Discount factor | Configurable and run-frozen; 0.9 proposed starting point |
| Value estimate | Codex scalar prediction of future net utility change |
| Search modification depth limit | D=8 from a fixed local root |
| Value target | Expected discounted continuing improvement under the declared policy; no remaining-budget input |
| Per-trial computation | Up to three new implementation attempts |
| Intermediate evaluation | Full Screen, targeted checks, and a fixed half of Selection S0 (proposed evaluation configuration) |
| More expensive evaluation | Full Selection for promising or uncertain candidates |
| Confirmation | Shortlisted promotion or final-output candidates |
| Final output | An existing evaluated harness, including any intermediate candidate |

**Configured first controller:** fixed branching with up to three distinct plans per expanded harness and up to three implementation attempts per plan, generated incrementally. Use 30 trials, each with up to three new attempts. Compare one or two implementations per plan, double progressive widening, and adaptive branching as alternatives under matched total cost. Section 8 specifies the proposed half-Selection evaluation and six-device scheduler.

## 2. Goal, utility, and the controller's surrogate objective

The practical goal is to discover a strong harness within a finite total search budget. Let the archive contain every generated artifact and let B bound generation and evaluation expenditure. The desired discovery objective is:

$$
\max_{\mu}\mathbb E\left[\max_{H\in\mathcal A_B}U(H)\right]
$$

Here mu is the overall allocation policy, and the archive includes the initial harness. Costs include proposer, policy, value, implementation and judge calls, benchmark execution, and other relevant resources. The initial reward does not subtract cost; total expenditure is constrained and recorded separately.

For an initial task-success utility:

$$
U(H)=\mathbb E_{\tau\sim\mathcal D_{\mathrm{Selection}},\,\omega}
\left[\mathbf 1\{\text{H succeeds on task }\tau\text{ under randomness }\omega\}\right]
$$

Thus U lies between zero and one. The task distribution, instances, success validators, underlying agent model, runtime limits, and aggregation weights must be specified. Small-subset measurements estimate a development objective; Confirmation supplies separate validation evidence.

**Important distinction:** the approved reward in Section 4 sums to final-descendant utility change, not the maximum utility encountered along a path. Consequently, it is a surrogate for best-artifact discovery. Preserving intermediate harnesses avoids losing a good artifact, but does not make the two mathematical objectives identical. This distinction should remain visible in experiments and claims.

## 3. State, action, transition, and persistent tree

### 3.1 Exact artifact state and search context

The artifact state is:

$$
s_t=H_t
$$

A search node additionally contains evidence, depth, and controller records:

$$
n_t=(H_t,E_t,d_t,R_t)
$$

E includes task results, trajectories, diagnostics, and relevant previous attempts. R includes plans, priors, child identities, visits, returns, and assessment provenance. The discounted continuing target is independent of the search depth cutoff, and Codex predictions depend on supplied evidence; therefore H alone does not describe every input to the controller. No claim is made that code alone is a sufficient Markov state for the complete search process.

Evaluating a stored harness adds evidence without changing its code. Editing the code creates a new artifact. Preserve exact snapshots; a filename or mutable working directory is not a sufficient identity.

### 3.2 Action: frozen `PLAN.md` contents

A modification action is a concrete intervention specification:

$$
a=(\text{target component},\text{operation},\text{conditions/parameters},\text{scope})
$$

Its supporting hypothesis identifies the observed failure, a candidate cause, and a predicted observable effect. Root causes remain hypotheses until supported by tests.

Each plan must connect:

$$
\text{failure evidence}\rightarrow\text{causal hypothesis}\rightarrow
\text{intervention}\rightarrow\text{predicted effect}\rightarrow\text{verification}
$$

It records evidence, target and scope, implementation constraints, expected code-path or trajectory effects, and checks for alignment and regressions. Prefer distinct interventions over paraphrases. Freeze a selected plan; material revisions create new action versions with provenance.

The same plan at different harnesses belongs to different state-action pairs. A different explanation need not create a different intervention branch, but all context supplied to Codex must be preserved because it can change the implementation.

### 3.3 Stochastic implementation transition

$$
H_{t+1}\sim T_{\mathrm{Codex}}(\cdot\mid H_t,a_t,E_t)
$$

Repeated implementation of the same parent-plan pair may produce different patches. This is stochastic generation, not automatically partial observability: the resulting artifact is stored and observable. No geometric action distance or state abstraction is assumed.

The persistent tree alternates between harness nodes and plan nodes. A plan may have several exact implementation children. Each child has its own subsequent plans. Plan nodes do not add modification depth.

Three operations must remain distinct:

| Operation | Effect |
| --- | --- |
| Traverse a stored child | Reuse the exact artifact and continue its subtree |
| Implement a plan again | Sample and store another child of its original parent |
| Reevaluate a stored child | Gather more evidence about the same artifact |

Selecting a stored child never implicitly reruns Codex.

## 4. Reward and Codex value estimator

### 4.1 Reward and frozen utility

The implemented utility is `F(H) = (1-lambda) U_S0(H) + lambda J(H)`, with
`lambda=0.1` by default, frozen per run. Setting lambda to zero gives the
AndroidWorld-only comparison. Immediate rewards remain:

$$
r_t=F(H_{t+1})-F(H_t).
$$

Improvement is positive; regression is negative. An intermediate decrease does
not automatically terminate exploration. Undiscounted immediate differences
still telescope to final minus initial score, but discounted returns do not.
We do not sum raw harness scores or change measured performance records.

### 4.2 Discounted continuing prediction target

Codex predicts expected discounted future net change under the declared
continuation procedure pi, without an explicit remaining-budget input:

$$
V^\pi(H,E)=\mathbb E_\pi\left[
\sum_{t=0}^{\infty}\gamma^t(F(H_{t+1})-F(H_t))\mid H_0=H,E
\right],\qquad 0<\gamma<1.
$$

`discount_factor=0.9` is the proposed experimental starting point. Discounting
favors earlier improvement; its geometric weighting has an effective span of
roughly ten transitions without a hard prediction endpoint. The target depends
on the proposer, selection, implementation, evaluation, judging, and evidence
updates in the supplied continuation policy. Proposer and policy roles receive
the same discount factor and continuation-policy definition/identity as value.
A discount step counts one harness transition, including stored traversal, not
failed attempts, token expenditure, or wall-clock time. It is policy expectation, not
unrestricted optimal harness quality, absolute utility, or best-case potential.

Because F lies in [0,1],

$$
V^\pi(H,E)=-F(H)+(1-\gamma)\mathbb E_\pi\left[
\sum_{k=1}^{\infty}\gamma^{k-1}F(H_k)\right],
\qquad -F(H)\le V^\pi(H,E)\le1-F(H).
$$

Negative predictions are valid. Actual depth and global run budgets stop
execution, not the continuing target. Preserve used local branching capacity,
traverse eligible stored children, and give future nodes their own local slots.
Plans and implementations evolve under the same procedure; new canonical
evidence updates priors and discounted statistics. Pending work and assessment
outages require uncertainty or recovery, not terminality. After genuine
structural exhaustion, utility remains constant and future rewards are zero.

### 4.3 What Codex returns

Inputs bind the exact artifact, objective, score, evidence and capacity
snapshots, discount factor, and continuation-policy identity. There is no finite
value horizon or remaining-depth field. An illustrative response is:

```json
{
  "artifact_id": "exact-artifact-id",
  "status": "completed",
  "score_snapshot_id": "score-id",
  "capacity_snapshot_id": "capacity-id",
  "objective_id": "objective-id",
  "discount_factor": 0.9,
  "continuation_policy_id": "policy-id",
  "value": 0.04,
  "rationale": "Expected early repairs outweigh discounted later regressions.",
  "uncertainty": "High: few task observations and an unvalidated continuation.",
  "evidence": ["context:score_snapshot_id"]
}
```

A value of 0.04 predicts discounted net improvement, not a particular final
utility after a fixed number of modifications. Only the scalar enters backup;
rationale and uncertainty are retained for review. A prediction is never stored
as measured benchmark performance. Version the target and backup contract;
older undiscounted N/W/Q cannot be mixed with these continuing returns.

## 5. Component responsibilities

| Component | Inputs | Output and responsibility |
| --- | --- | --- |
| Action proposer | Harness, failures, trajectories, relevant attempts | Distinct causal hypotheses and executable modification plans |
| Policy estimator | Candidate plans, harness context, supporting and contradictory evidence | Normalized plan priors and priority rationales |
| Implementation agent | Exact parent, frozen plan, approved context | Exact child artifact, diff, logs, and implementation status |
| Task evaluator | Exact artifact, versioned tasks and execution configuration | Success outcomes, cost, trajectories, and diagnostics |
| Value estimator | Frontier artifact, evidence, F snapshot, discount, continuation policy | Expected discounted continuing net utility change |
| Search controller | Priors, branch statistics, budget and depth | Traversal, expansion, resampling, evaluation allocation and backup |

### Policy estimator and PUCT

The policy estimator returns nonnegative priors that sum to one:

$$
\sum_{a\in A(n)}P(a\mid n)=1
$$

Assess causal support, plausible impact, feasibility, regression risk, transfer, and cost. These are allocation preferences, not calibrated causal beliefs or success probabilities.

The initial controller selects:

$$
a^*=\arg\max_a\left[
Q(n,a)+c_{\mathrm{puct}}P(a\mid n)
\frac{\sqrt{N(n)}}{1+N(n,a)}
\right]
$$

Unvisited plan edges start with Q=0 and N=0. Zero denotes initialization. At a parent with zero visits, explicitly choose the highest-prior eligible plan with deterministic tie-breaking. Later selections use PUCT. Priors can be outweighed by observed outcomes.

**Engineering detail:** use consistent node/edge visit definitions and exclude exhausted or otherwise ineligible branches. Choose and log c_puct. If widening adds plans, version and renormalize priors rather than silently changing their interpretation.

## 6. Tree branching: three alternatives

### 6.1 Fixed branching

Limit each expanded harness to three feasible plans. The configured implementation cap in this note is three per parent-plan pair:

$$
K_{\mathrm{plans}}=3,\qquad K_{\mathrm{implementations}}=3
$$

Generate fewer plans if three distinct feasible interventions cannot be justified. Earlier one- and two-implementation settings remain useful lower-cost comparisons. The cap controls width; it is not a request to implement three outcomes simultaneously.

Fixed width is reproducible, but few outcomes may obscure a useful plan. The generation and stored-child selection rules are specified below.

#### Incremental generation and stored-implementation selection

Generate **one implementation at a time**, not a batch of all permitted outcomes. The implementation cap is local to an exact parent-plan pair. In the simple fixed-branching controller:

1. Select a plan using harness-level PUCT and Codex plan priors.
2. If its attempted implementation count, including invalid and pending reserved attempts, is below the cap and generation budget permits, atomically reserve a slot and generate, evaluate, and preserve one new child. Section 8 specifies the proposed attempt-accounting convention.
3. Otherwise select an eligible stored child using implementation-level PUCT and descend into that exact artifact.

Each generation uses one of the three new-attempt slots per trial. At the new child, the controller can immediately select that child's own plans. A revisit does not regenerate an existing artifact. Capacity limits in widening or adaptive branching permit resampling but do not force it; their allocation rule may continue a stored child before filling all slots.

For M eligible stored implementations of parent H and plan a, initialize uniform priors:

$$
P_{\mathrm{impl}}(i\mid H,a)=1/M
$$

Select:

$$
i^*=\arg\max_i\left[
Q_{\mathrm{impl}}(H,a,i)+
c_{\mathrm{impl}}P_{\mathrm{impl}}(i\mid H,a)
\frac{\sqrt{N_{\mathrm{plan}}(H,a)}}{1+N_{\mathrm{impl}}(H,a,i)}
\right]
$$

M is the number currently eligible, not the eventual cap. Recompute the uniform priors when that set changes. Keep implementation-level visits and returns separate from harness-plan statistics. Use a declared tie-breaking/first-visit rule when empirical values are unavailable; normally the generation trial's backup supplies the first return for a valid child.

**Value convention:** implementation Q is measured relative to the original plan's parent, so it includes both the implementation's immediate reward and its subsequent continuation return:

$$
Q_{\mathrm{impl}}(H,a,i)\approx
F(H_i)-F(H)+\gamma V^\pi(H_i,E_i)
$$

This expression specifies the return target; Q is the mean of backed-up trial returns, not a fresh Codex score recomputed at every selection.

If G_t is the return for the harness-plan edge whose selected implementation is i, update that implementation with the same G_t:

$$
N_{\mathrm{impl}}(H,a,i)\leftarrow N_{\mathrm{impl}}(H,a,i)+1,\qquad
W_{\mathrm{impl}}(H,a,i)\leftarrow W_{\mathrm{impl}}(H,a,i)+G_t,\qquad
Q_{\mathrm{impl}}=W_{\mathrm{impl}}/N_{\mathrm{impl}}
$$

Both bookkeeping levels receive one update per trial traversal. Do not add the immediate utility-change reward twice or count the plan-to-implementation bookkeeping step as another modification depth.

Uniform random outcome sampling would serve estimation of the stochastic implementation distribution. PUCT over stored children serves deliberate allocation to persistent artifacts with promising continuation returns. Thus plan Q summarizes returns under this allocation policy; it is not an unbiased estimate of a fresh random implementation's effectiveness. Current utility alone is not the selection criterion: a lower-scoring child can be a productive stepping stone.

The implementation-level exploration coefficient remains to be specified. Section 8 proposes counting invalid outcomes against attempt capacity while excluding them from scored continuation; assess this convention experimentally.

### 6.2 Double progressive widening

Allow plan and implementation branching to grow with visits:

$$
K_a(n)=\max(3,\lfloor k_a(N(n)+1)^\alpha\rfloor),\quad 0<\alpha<1
$$

$$
K_o(n,a)=\max(1,\lfloor k_o(N(n,a)+1)^\beta\rfloor),\quad 0<\beta<1
$$

A new plan may be admitted below K_a. Another Codex implementation may be sampled below K_o. Otherwise reuse stored children. These are capacity limits, not requirements to generate new branches.

The method handles open-ended proposals without needing geometry. Its weakness is that visit counts do not directly establish whether another generation deserves its cost.

**Interpretation:** empirical-frequency sampling of children estimates the implementation transition distribution. Deliberately choosing promising stored artifacts instead implements discovery-oriented allocation. In the latter case, Q summarizes returns under that allocation process; it should not be presented as an unbiased estimate of a freshly sampled plan's effectiveness.

### 6.3 Adaptive branching

Use outcome evidence and uncertainty to allocate among:

1. Proposing and implementing a new plan.
2. Resampling an existing plan at its original parent.
3. Continuing from an existing exact child.

AB-MCTS motivates feedback-driven generation versus descent. Our explicit plan/implementation separation is an adaptation, not an already specified component of that method.

A Thompson-sampling alternative compares sampled predictive outcomes for these allocation choices. Its predictive model and cost treatment must be specified before implementation. Do not assume that this alternative automatically uses the same PUCT formula. Compare complete controllers rather than mixing incompatible selection semantics without explanation.

**Deferred methods:** HOOT, Continuous RAVE, and abstraction refining require meaningful partitions or similarity representations that we have not validated for plans and harnesses. Equal scores or similar wording do not establish similar continuation value.

## 7. Horizon, trial length, and continuation

**Agreed settings:**

$$
D=8,\qquad L_{\mathrm{new}}=3,\qquad \gamma=0.9\text{ initially}
$$

The run allocates 30 trials, with at most 90 new implementation attempts in total. This is an upper bound on generation, not a promise of 90 valid children or independent measurements. Two asynchronous workers reserve trial IDs from one shared counter; no worker launches trial 31.

The local root has depth zero. Every stored parent-child harness transition adds one depth level. Resampling a plan creates a sibling at the same depth as its other implementation outcomes. Plan nodes add no depth.

A trial starts at the root, traverses existing branches, and creates up to three new implementation attempts along its selected continuation. Stored traversal consumes depth but no new implementation slots. Invalid attempts consume computation slots but do not create a valid continuation state.

For example, frontiers at depth three, five, and eight all request expected discounted continuing improvement under the same frozen discount and procedure. There is no remaining-budget input.

At depth eight, create no further modification children, but still invoke the value estimator for the discounted continuing target and back up its bounded prediction. For forecasting only, allow counterfactual continuation beyond the search depth limit using the same policy and branching constraints. Return to root selection after the trial. Preserve all intermediates. Stop early for budget limits, invalid continuation, or no feasible next plan; do not stop solely because one reward is negative.

This is **multi-step persistent tree expansion with frontier bootstrapping**, rather than a separate random rollout. The same controller chooses continuation plans after fresh evaluation evidence is available.

**Proposed terminal convention:** if a valid branch genuinely cannot continue, treat its utility as constant thereafter and use zero future change. Distinguish this from a computational cutoff, where bootstrap applies. Explicit stop actions and handling invalid implementations remain open.

## 8. Evaluation design and six-device execution

### Operating configuration and status

Use 30 trials, up to three new implementation attempts per trial, three plans per harness, and up to three implementation attempts per plan. Actual search depth remains capped at eight; the continuing prediction uses gamma=0.9 by default and has no finite horizon. Generate implementations incrementally; these caps do not require eagerly generating nine children at each harness.

**Recommended evaluation configuration:** freeze half of Selection for intermediate rewards and run two asynchronous trial workers sharing a pool of at most six devices. The half-Selection fraction and scheduler are the concrete proposal following the user's request; exact task membership, runtime limits, and validation budget must be recorded before execution.

Thirty trials permit at most 90 new implementation attempts, not 90 guaranteed valid children. Stored-child traversal creates no new implementation. A trial can finish after fewer attempts because of depth, failure, or budget limits. Record every launched trial, including failed or empty trials; do not quietly replace failures with unlimited additional trials.

### Evaluation stages

| Stage | When | Purpose and accounting |
| --- | --- | --- |
| Full Screen | Root and every newly generated child that can execute | Eligibility and basic functionality. Reference time: approximately 20 minutes with three devices. |
| Targeted checks | Every new child, as applicable to its plan | Mechanism evidence; record additional cost separately. Targeted success is not the overall reward. |
| Fixed half-Selection S0 | Root and every Screen-valid new child | Current utility, immediate reward, and continuation evidence. Half the tasks need not mean half the runtime. |
| Full Selection | Evidence-triggered candidates and final shortlist | Stronger development measurement. User-provided reference: up to three hours with three devices. |
| Confirmation | Frozen final shortlist and baseline | Protected validation; exclude results and trajectories from routine search feedback. |

Passing Screen means passing a declared eligibility rule, not solving every Screen or Selection task. Declare that rule before the run. Ordinary benchmark task failures receive their defined outcome; evaluator/device outages are infrastructure failures and must not be converted into task failures.

### Constructing and freezing the half-Selection subset

Let S be the versioned full Selection task manifest and choose |S0|=ceil(|S|/2). If S contains 20 tasks, use 10; the actual manifest count must be checked. Use a recorded seed to sample within task strata covering apps, task types, multi-step difficulty, and important failure modes. Use baseline metadata to construct strata, not candidate performance to cherry-pick tasks. Publish selected and remaining task IDs, stratum counts, aggregation weights, and sampling seed in the run manifest.

Use the same S0 throughout all 30 trials. Each child evaluates the entire S0, not a fresh random half. This supplies matched comparisons and keeps backed-up rewards on one objective. It also creates a development set that can be overfit; full Selection and protected Confirmation address different parts of that limitation.

For completion-rate utility, store each task outcome y and use the predeclared weighted mean:

$$
\widehat U_{S_0}(H)=\sum_{j\in S_0}w_j y_j(H),\qquad \sum_j w_j=1
$$

Use equal weights if the benchmark objective is an unweighted mean and sampling preserves its composition adequately. If stratification changes proportions, predeclare weights reflecting the full Selection objective. Report both subset and full-set coverage. Half is a starting allocation, not a statistical guarantee: a small or highly correlated task set may still rank close candidates poorly.

Freeze model version, evaluator, task instances, reset procedure, limits, and controllable seeds. Store per-task trajectories and scores. Cache by exact artifact plus this complete execution configuration; parent or stored-child traversal normally reuses those records. A new execution configuration requires a matched comparison or separate statistics.

### Reward, value prediction, and fidelity

Search uses:

$$
\widehat r_t=\widehat F(H_{t+1})-\widehat F(H_t),\qquad 0<\gamma<1
$$

Screen and targeted checks are gates or diagnostics, not additive rewards. Negative measured reward is permitted. Do not subtract a full-Selection parent score from a half-Selection child score.

Codex's frontier value must predict discounted continuing net change using the same frozen F as immediate rewards and Q:

$$
\widehat V(H,E)\approx
\mathbb E_\pi[\sum_{t\ge0}\gamma^t(\widehat F(H_{t+1})-\widehat F(H_t))\mid H,E]
$$

Supply subset/objective/score/evidence identities, task and judge weights, evaluation conditions, discount factor and continuation-policy identity. Since F lies in [0,1], the value range is [-F(H),1-F(H)]. At depth eight still forecast continuing discounted improvement; do not force zero because actual expansion stops. Current completion rate is already accounted for by utility-change rewards; do not add it to the frontier value.

Maintain separate Screen, S0, full-Selection, and Confirmation records. Full Selection can inform development hypotheses and a logged promotion decision, but does not silently overwrite Q rewards. If the subset, weighting, or objective changes, start a new statistics version or explicitly rebuild backups. Section 9's reverse-backup equations remain unchanged.

### Full Selection and final validation

**Proposed defaults:** nominate up to three distinct artifacts by measured S0 utility at search completion, together with the root baseline. Preserve structural diversity as a tie-breaker and log it. During search, allow a full-Selection promotion when a candidate becomes a new provisional incumbent or a close comparison would determine a large continuation allocation; queue promotions and enforce the reserved evaluation budget rather than evaluating every small improvement automatically.

Complete full Selection for the nominees, then rank by its measured objective with uncertainty and regressions reported. A high Q is insufficient because Q includes predicted future improvement. If a nominee's full score reveals a discrepancy, log it and use the remaining preallocated development budget; never treat Confirmation as repair feedback.

When full Selection uses precisely the same task instances and evaluation conditions, reuse the candidate's existing S0 outcomes and evaluate S minus S0. Otherwise run a complete fresh matched evaluation and label it as such. Aggregate individual task outcomes with full-set weights; do not average the two half means unless task counts and weights justify that operation.

Freeze the final shortlist before Confirmation. Confirm it and the baseline under a common protocol, retaining every confirmed result. Choosing the winner using Confirmation makes that set part of model selection; an independent final performance claim requires an untouched test or later fresh evaluation.

**Proposed budget reservation:** reserve 20–30% of total device-hours for broader Selection, repeats, baseline comparisons, and Confirmation, then check that this covers the actual shortlist cost. This percentage is a planning default, not an agreed or sufficient budget. Confirmation duration has not been supplied. If 30 trials plus validation exceed available device-hours, reduce intermediate expansions or extend the run; keep the trial count, actual attempts, and validation coverage explicit.

### Parallel trial and device scheduler

Start with two asynchronous trial workers, each usually requesting three devices for its current evaluation. Trial 2 need not wait for trial 1's full continuation or backup. Within one trial, the next modification depends on its child's evidence; evaluate that child before proposing the next continuation. Three sequential modifications cannot be evaluated concurrently as if they were independent.

Use one device pool and per-task job queue, not two permanently partitioned device groups. Each job carries artifact/configuration identity, stage, task ID, isolated reset requirements, estimated runtime, deadline, and trial ID. Assign individual tasks to free compatible devices; allow one evaluation to borrow idle capacity while the other worker is implementing or waiting. Six devices can accelerate a single batch when tasks are independent, but speedup depends on task count, runtime imbalance, and reset overhead.

For long Selection batches, prioritize long tasks using measured historical durations to reduce the tail, with fairness across trials so one batch cannot starve the other. Screen-valid children enter S0; rejected children release remaining jobs and reservations. Keep immutable code/configuration per task and reset app/device state between runs to prevent cross-artifact contamination. Pipeline Codex generation and assessment with unrelated evaluation jobs where independent.

Do not increase active trials solely to fill six devices: begin with two, measure device utilization and pending-selection collisions, and increase worker concurrency only if generation waits leave sustained idle devices and the evaluation queue can support more workers.

### Concurrent tree safety and exactly-once backup

Protect only short tree operations with a lock or transaction; never hold it during Codex calls or task evaluation. Atomically reserve trial IDs 1–30 and plan implementation slots. Count successful, failed, and currently reserved implementation attempts against the plan cap of three. Two workers may intentionally sample the same plan in different reserved slots, but must not accidentally produce the same slot twice.

Track pending path reservations separately from completed visit counts. **Proposed initial scheduling rule:** prefer an eligible unreserved edge when alternatives exist; if all useful edges are reserved, wait for a result rather than repeatedly launching duplicate continuations. Apply this at plan and implementation selection. A recorded pending-count exploration adjustment is an alternative for later experiments, not measured reward.

Use completed N, W, Q for backed-up evidence. Do not count a pending trial as a completed visit or assign a fabricated negative return. Release reservations on completion, cancellation, or failure. A shared stored implementation can be revisited after release; cache reuse is not another independent benchmark observation.

Each trial logs the ordered valid path, exact reward snapshot IDs, frontier prediction, and unique backup ID. Commit reverse backup atomically once: both selected plan and implementation receive the corresponding parent-relative G_t once, while the reward and depth are counted only once. Retried persistence must not repeat the backup. Persist reservations and checkpoints so interrupted jobs can resume or be explicitly cancelled.

### Failure and reevaluation policy

**Proposed initial convention:** an invalid implementation consumes its attempt and plan slot and cannot be traversed; log it without inventing S0 utility. End that trial's continuation and bootstrap from its last valid frontier, backing up only existing valid transitions. If there is no valid traversed transition, record a failed trial without a Q update. This means invalid-attempt rate is not directly penalized in Q; report it separately and assess whether a later feasibility/cost model is needed.

Infrastructure faults retry the affected task once by default after reset; repeated infrastructure faults mark the evaluation incomplete and quarantine the device or job for investigation. A genuine agent failure or timeout uses the benchmark's declared score. Incomplete children stay ineligible for scored traversal until evaluation is completed or the attempt is cancelled. Do not fabricate missing outcomes.

Keep one fixed, versioned evaluation snapshot per artifact during the initial search. Extra repeats and full-Selection measurements are separate evidence; adopting a corrected S0 score requires a declared statistics rebuild. Repeated tree visits using cached outcomes do not increase the benchmark sample size.

### Runtime planning and monitoring

With three devices, the supplied reference timings are Screen approximately 20 minutes and full Selection up to 180 minutes. If runtime scales roughly with task count, half Selection is approximately 90 minutes and a valid child's evaluation approximately 110 minutes. This half-set estimate must be checked on the baseline; it is not a measured upper bound.

For 30 trials each creating three valid children, two concurrent evaluations give the reference scenario:

$$
90\text{ children}\times110\text{ minutes}/2
=4{,}950\text{ minutes}=82.5\text{ hours}
$$

That is roughly 3.4 days of intermediate evaluation under full utilization and the stated scaling assumptions. It excludes root evaluation, targeted checks, Codex time, resets, queue imbalance, retries, full-Selection promotions, repeats, and Confirmation. Fewer expansions lower cost. A cached other-half full-Selection completion costs approximately another 90 minutes per candidate with three devices only under the same linear assumptions.

Record device-hours as well as wall time. Using six devices for one batch rather than two groups of three can lower that batch's latency without improving total throughput. Measure baseline Screen and S0 duration distributions first, then revise the schedule and total cost estimate.

Dashboard/log essentials: trials launched/completed/failed; attempts and valid children; device utilization; queue wait; per-stage task durations; device-hours by stage; completed versus pending tree visits; provisional and full utility of incumbents; value-prediction contributions; validation budget remaining. Report actual coverage and costs alongside the final artifact.

### Agent-as-a-Judge alongside per-child Android evaluation

**Agreed initial direction:** run a bounded reviewer assessment for every newly implemented child, in parallel with Android evaluation where inputs are available. Review each exact artifact once per evidence/configuration version; a stored-node revisit reuses the assessment. For an invalid child, retain any available code-level findings, but do not invent execution evidence.

The Android evaluator measures task outcomes and current utility. The agent judge evaluates whether the intervention follows the frozen plan and whether observed execution supports its hypothesis. These are separate records and roles.

#### Inputs and review dimensions

Give the reviewer the frozen PLAN.md, exact parent and child artifacts, diff, relevant parent failures, targeted-check results, and available child trajectories. Use a separate reviewer context from the implementer and a fixed rubric. Confirmation evidence remains excluded.

| Dimension | Review question | Required evidence |
| --- | --- | --- |
| Plan alignment | Does the executable implementation realize the proposed intervention? | Plan requirement and specific code/configuration references; identify missing or contradictory behavior. |
| Mechanism activation | Did the changed behavior execute, and is its effect consistent with the hypothesis? | Relevant trajectory events and code paths; distinguish predicted behavior from observed activation. |
| Generalization and task specificity | Is the repair reusable, or dependent on one task's names, coordinates, or sequence? | Specific conditions, hardcoded assumptions, and available cross-task evidence. A general-looking patch alone does not establish transfer. |
| Regressions | Does the patch introduce conflicting behavior or remove useful functionality? | Diff and execution evidence, with affected components and tasks identified. |

Example: task success after a recovery patch does not prove the recovery mechanism was used. The judge should report whether the modified recovery path activated, what happened next, and what remains unverified. Without a controlled comparison, mechanism support is not proof of causality.

#### Parallel workflow and cost limits

1. After storing the immutable child, start code/plan review while Android Screen and applicable targeted tests execute.
2. As child trajectories become available, supply them to the same bounded review job and finalize execution-grounded findings after the relevant S0 evidence arrives. Trajectory review cannot finish before that evidence exists.
3. Attach completed findings to the child's development evidence for subsequent plan proposal, policy assessment, and frontier value prediction.

Set a fixed per-child call/token and wall-time budget before the run. Exact limits remain an engineering parameter. Initially inspect relevant trajectories within this budget rather than launch an unbounded investigation or extra device experiments. Explicitly requested follow-up tests use the existing evaluation queue and have separate costs.

**Proposed scheduling default:** wait for the finalized review only within its declared deadline before the next evidence-consuming Codex call; on timeout proceed with available findings marked partial. Judge failure or delay must not turn a measured Android result into a task failure. Later findings apply to future decisions; they do not trigger another backup for the completed trial.

#### Explicit output and evidence discipline

Produce a structured assessment containing artifact/plan IDs, reviewer and rubric versions, evidence snapshot IDs, and one finding per dimension. Each finding includes an assessment such as supported, contradicted, or insufficient evidence; cited code locations/trajectory events; a concise rationale; and uncertainty. Record suspected issues separately from observed violations, plus prioritized next checks and total cost. These labels are qualitative findings, not calibrated probabilities.

Require evidence references for substantive claims and allow insufficient evidence. Treat the implementer's explanation as a claim to check. Keep observed facts, hypotheses, and recommended tests distinct. Version the assessment when new evidence arrives; cache reuse is not an independent judge vote.

#### Integration with search

Retain the approved utility-change reward and reverse backup unchanged:

$$
r_t=\widehat U_{S_0}(H_{t+1})-\widehat U_{S_0}(H_t)
$$

Judge findings may inform the next proposer, policy estimator, and Codex value assessment as explicitly labeled evidence. Do not add a judge score to reward, substitute it for Android completion outcomes, or create an extra N/W/Q update. The frontier value remains expected future net utility change on S0, not a plan-quality score.

Subjective concerns prompt investigation rather than automatic rejection. Any hard eligibility rejection must follow a predeclared rule supported by verifiable evidence. Log judge/evaluator disagreement and examine it without silently overriding benchmark outcomes.

#### Evaluation of the judge itself

For the 30-trial experiment, record per-child judge costs and findings and assess whether they predict full-Selection regressions, identify implementation–plan mismatches, and help subsequent interventions. Audit a sample manually for unsupported claims and missed problems. Compare search with and without judge feedback under matched total cost; predictive correlation alone does not prove that feedback improves search.

The initial role is now specified; reviewer model, exact resource limits, trajectory-selection policy, and audit rubric remain open. Agent-as-a-Judge: Evaluate Agents with Agents motivates intermediate evidence-based evaluation, but its code-generation findings do not establish reliability in this mobile harness setting.

## 9. Bootstrap and backup

A trial records harness transitions and their matched reward estimates. At frontier H_L:

$$
\widehat v_L=
\begin{cases}
\widehat V^\pi(H_L,E_L,8-d_L),&d_L<8\text{ and continuation is possible}\\
0,&d_L=8\text{ or a defined terminal stop}
\end{cases}
$$

Back up in reverse order:

$$
G_L=\widehat v_L,\qquad G_t=\widehat r_t+\gamma G_{t+1}
$$

For each traversed harness-plan edge:

$$
N(n_t,a_t)\leftarrow N(n_t,a_t)+1
$$

$$
W(n_t,a_t)\leftarrow W(n_t,a_t)+G_t,\qquad
Q(n_t,a_t)=W(n_t,a_t)/N(n_t,a_t)
$$

Plan nodes introduce no extra reward or discount step. Record node visits consistently. For each selected implementation, also update its own N_impl, W_impl and Q_impl with the same parent-relative G_t used for that plan edge. These are two bookkeeping updates of one return, not two rewards.

Include cached transition rewards for stored edges traversed during the trial. Reusing an edge in a new trial contributes that trial's continuation return; it does not constitute a new independent benchmark evaluation. Each edge receives one update per trial, not intermediate updates plus another duplicate final update.

### Numerical example

For an AndroidWorld-only comparison (judge weight zero), starting from root utility 0.60, generate:

| Harness | Small-subset utility | Reward |
| --- | --- | --- |
| H0 | 0.60 | — |
| H1 | 0.65 | +0.05 |
| H2 | 0.62 | -0.03 |
| H3 | 0.70 | +0.08 |

At depth three, Codex predicts discounted continuing value 0.04. With the illustrative gamma=0.9, returns are:

$$
G_2=0.08+0.9(0.04)=0.116,\qquad
G_1=-0.03+0.9(0.116)=0.0744,\qquad
G_0=0.05+0.9(0.0744)=0.11696
$$

These update the three respective plan edges. For a first visit, Q equals that return; later it is the average of accumulated returns. They represent changes relative to each edge's parent. The frontier score 0.70 is not added as an extra reward.

For a path with L explicit transitions:

$$
G_t=\sum_{k=t}^{L-1}\gamma^{k-t}\widehat r_k+\gamma^{L-t}\widehat v_L
$$

This makes measured versus predicted contributions explicit. Across trials, keep the discount, utility, and continuation-policy identities consistent. These allocation returns are distinct from best retained artifact performance.

## 10. Controller procedure and implementation records

The following pseudocode describes the logic inside one trial worker of the fixed-branching controller with incremental implementation generation and two-level PUCT. Run up to two workers under Section 8's shared trial counter, device queue, slot reservations, and atomic backup rules. K_implementation is the configured cap (currently three in this note). Widening and adaptive variants retain their own resampling-versus-continuation allocation rules.

```
coordinator evaluates root once on Screen and common subset S0
coordinator stores immutable root and initializes shared tree

# two workers execute the following loop
while budget permits and atomically reserve_next_trial_id(maximum=30):
    node = root
    path = []
    new_attempts = 0

    while node.depth < 8:
        register up to 3 distinct plans if needed
        obtain/version normalized plan priors
        atomically choose eligible plan and reserve path/attempt slot if needed
        # recheck capacity in the same transaction; wait/reselect on contention

        if a new implementation-attempt slot was reserved:
            if new_attempts == 3 or generation budget is insufficient:
                break
            i = reserved implementation-attempt slot ID
            implement frozen plan on exact parent once
            new_attempts += 1
            store attempt, artifact, diff and logs
            launch bounded judge code/plan review in parallel
            run Screen and targeted checks
            if invalid:
                record failure; invoke declared failure/capacity policy
                break
            evaluate child on S0; stream relevant trajectories to judge
            finalize judge within its deadline, or record partial/unavailable findings
            attach labeled judge findings to development evidence
            register this exact child as implementation i of the plan
        else:
            if no eligible stored implementation exists:
                invoke declared failure policy
                break
            i = implementation_PUCT(plan, uniform_priors)
            child = exact stored artifact of implementation i

        reward = cached matched frozen combined F difference(child, node)
        append (node, plan, i, child, reward) to path
        node = child

        if new_attempts == 3:
            break

    if path is nonempty:
        frontier_value = 0 if structurally_exhausted else CodexValue(node, discount_factor, continuation_policy_id)
        reverse-backup G = reward + discount_factor * next_G exactly once
        update both plan-edge and selected-implementation statistics with each G_t

    allocate full Selection when evidence triggers it
    record costs and persist trial state

# coordinator, once all workers have joined and reservations are released
shortlist measured candidates and complete full Selection
run Confirmation on frozen shortlist and baseline
return an exact stored artifact with its evaluation report
```

A declared failure policy is required before this loop is production-ready. An invalid patch must not receive a fabricated Selection score or disappear from the cost ledger. Distinguish harness failures from infrastructure failures such as an unavailable emulator or broken evaluator.

### Required records

| Record | Minimum contents |
| --- | --- |
| Harness | ID, immutable snapshot/hash, parent, plan and attempt IDs, depth, runtime/model configuration |
| Plan | Frozen contents, hypothesis, evidence, constraints, expected outcomes, version |
| Attempt | Parent, plan, implementation configuration, diff, validity, logs, cost |
| Evaluation | Artifact ID, stage, task/instance IDs, conditions, validator version, outcomes, trajectories, cost |
| Judge assessment | Artifact/plan IDs, reviewer/rubric version, evidence snapshot IDs, findings and references, uncertainty, partial status, next checks, cost |
| Policy assessment | Candidate IDs, priors, rationales, evidence/configuration versions |
| Value assessment | Artifact, F snapshot/fidelity, discount, continuation-policy identity, scalar, uncertainty, rationale |
| Trial and backup | Ordered path, reward sources, frontier estimate, returns, visits and observed/predicted contributions |

**Proposed engineering safeguards:** validate normalized priors, value bounds, artifact identity, and discount/policy consistency. Cache evaluation by artifact and execution configuration. Persist after each costly operation so interruption does not lose evidence.

If reevaluation revises a score, do not rewrite historical rewards invisibly. Keep versioned assessment snapshots; choose a declared correction/replay policy rather than appending corrected evidence as an extra search visit.

## 11. Validation plan and unresolved decisions

### Research comparisons

Compare methods under matched total generation and evaluation cost, including Codex assessment calls. Report the best confirmed existing harness, cost-to-improvement, valid implementation rate, regressions, and performance across task types.

Priority comparisons are fixed branching m=1, m=2, and configured m=3; widening versus fixed width; bootstrap versus no bootstrap; staged versus fuller evaluation; and adaptive versus fixed continuation length. These are proposed experiments, not evidence of superiority.

Evaluate value predictions against realized discounted reward prefixes plus appropriately discounted continuation tails. Distinguish truncated observations from genuine termination; a finite prefix is not the complete continuing target. Assess ranking usefulness, optimistic bias, subjective uncertainty, and whether adding predictions improves allocation under cost. A change in continuation policy changes the prediction target and should be logged.

### Decisions still required before a complete implementation

| Decision | Why it matters |
| --- | --- |
| S0 task membership, stratification and weights | Half-Selection is the proposed fraction; freeze the versioned task manifest, sampled task IDs, and objective weights before execution |
| Full-Selection triggers and repeats | Controls expenditure and stronger evidence |
| Validate proposed invalid-patch and infrastructure-failure conventions | Section 8 supplies initial rules; their effect on feasibility allocation and missing outcomes must be tested |
| c_puct and prior refresh rules | Determines exploration and comparability |
| Implementation exploration coefficient and widening allocation | Stored-child PUCT is defined; resampling-versus-continuation policies for wider controllers still need specification |
| Early stopping/terminal convention | Defines value when continuation cannot proceed |
| Score revision and uncertainty handling | Prevents inconsistent or overconfident Q statistics |
| Judge resource limits, reviewer model and audit rubric | Per-child parallel diagnostic review is specified in Section 8; calibrate reliability and cost before relying on its findings |
| Final shortlist and independent test protocol | Determines what final claims are supported |
| Surrogate versus discovery objective | Tests whether terminal-gain allocation finds the best retained artifacts |

D=8 and three new attempts per trial are initial experimental settings. Their utility, Codex's long-horizon calibration, and the value of deeper continuation require empirical evaluation.

## 12. Reference context

The concrete decisions in this note derive from the project discussion in Implementation Idea.

The following are reading references retained from that note, not claims that the proposed architecture exactly reproduces their algorithms:

- Continuous Upper Confidence Trees: reference for controlling action and stochastic-outcome branching.
- Wider or Deeper? Scaling LLM Inference-Time Compute with Adaptive Branching Tree Search: reference for adapting generation versus continuation.

The proposed system combines exact-artifact persistence, explicit plan actions, staged task evidence, and bootstrapped utility-change returns. Its empirical value and any methodological novelty must be established through the comparisons above.