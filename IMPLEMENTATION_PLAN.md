# AlphaAgents fixed-PUCT v1 implementation plan

## 1. Goal, scope, and operating defaults

Implement the persistent harness-and-plan tree described in `alpha_agents.md`, using the existing repository’s evaluation and Codex infrastructure.

The first release must support a complete run: baseline evaluation, frozen plan generation, stochastic implementation, staged evaluation, bounded judging, two-level PUCT, frontier prediction, transactional backup, interruption recovery, and final artifact selection.

The following table separates user-selected behavior from engineering and experimental defaults. This is a reviewed design plan; no implementation is authorized by this document alone.

| Area | v1 decision |
|---|---|
| Compatibility | Add the new controller alongside DGM; preserve existing APIs, configurations, and archives |
| First live integration | Artemis through the trusted local AndroidWorld runner |
| Search controller | Fixed branching with two-level PUCT |
| Horizon | Eight harness modifications from a fixed root |
| Trials | At most 30 launched trials, including failed and empty trials |
| New attempts | At most three per trial; at most 90 overall |
| Branch limits | Exactly three frozen plans per expanded harness; three implementation slots per parent-plan pair, filled through MCTS |
| Trial workers | Two asynchronous workers |
| Device scheduling | One shared queue serving up to six configured devices |
| Screen gate | Current live runs freeze `execution_valid`; `strict_all_success` remains an optional experimental configuration |
| Search utility | Combined harness score `F(H) = (1-lambda) U_S0(H) + lambda J(H)`; `lambda=0.10` is an experimental default for review |
| Discount | One |
| Judge | Score the harness itself, using code-quality/behavior review during Android evaluation followed by trajectory-grounded review; no plan-alignment dimension; two hours per invocation |
| Codex roles | Five explicit roles: proposer, policy estimator, implementer, value estimator, and judge; separate contexts using the same configured model/effort |
| Current Codex experiment setting | `gpt-6-luna` with `low` reasoning effort for all five roles, as requested for the initial trial |
| Prompt design | A primary quality requirement: sophisticated, detailed, carefully authored role prompts with explicit autonomy, evidence, workflow, completion, and output requirements; do not abbreviate them |
| Implementer execution | Native Codex `/goal`: persist the frozen-intervention objective and thread/goal record, continue through meaningful verification, and enforce one two-hour deadline across all internal continuation turns |
| Plan proposer mode/diversity | Native Codex `/plan` mode; review the existing plan archive and prior attempts before proposing substantively distinct interventions |
| Codex run duration | `codex_run_timeout_seconds = 7200` for every role invocation, including each judge phase and any permitted retry |
| Failed value assessment | Retry once, then stop new trial admission and retain the trial for recovery |
| Final winner | Select using full Selection; Confirmation is deferred and is not executed or budgeted in v1 |
| Resource limits | No device-hour cap or percentage allocation; retain trial/attempt/depth/concurrency/time limits and protected Codex completion invocations; report device-hours |
| Initial exploration coefficients | Experimental defaults: `c_puct = 1.0`, `c_impl = 1.0`; prior floor `epsilon = 0.01`; configurable, frozen, and recorded |

Progressive widening, adaptive branching, automated research comparisons, Mobile-Agent live integration, and Confirmation execution are deferred. Interfaces must permit their later addition without changing stored artifact identity or reward semantics.

For this version, final evaluation means full Selection on the shortlist and root baseline. The protected Confirmation task partition remains untouched and excluded from search evidence, but no Confirmation jobs are scheduled. A completed run produces a development-set winner; it does not establish independently confirmed performance. Device-hours are recorded and forecast for observation only: they do not block admission, stop trials, reduce the shortlist, or prevent final evaluation.

Operational values—model, endpoint, device serials, ports, interpreter, fixture identity, and Screen-gate policy—are required configuration inputs. Missing values fail preflight rather than receiving inferred defaults. The example run configuration uses the strict gate selected earlier in the discussion, but that choice is not an inherent requirement of the search method.

The discovery objective remains the best retained evaluated artifact. Search now uses changes in a combined AndroidWorld/harness-judge utility. This replaces the original AndroidWorld-only reward rather than adding a second backup or a bonus for following a plan. The proposed blend is 90% AndroidWorld and 10% judge; its weight is an experimental review default, not a calibrated ratio or a user-selected weight. Freeze the configured weight and rubric before search. Combined terminal-descendant returns remain a surrogate for best-artifact discovery. Final winner selection remains full-Selection AndroidWorld utility as previously agreed; report the judge and combined scores separately.

Prioritize the quality of diagnosis, intervention, implementation, and verification. Every Codex invocation receives up to two hours to complete its role-specific work; do not impose the earlier five-/ten-minute assessment deadlines. Time availability is not a quality guarantee: completion requires the evidence and checks defined for each role below, rather than merely producing an initial answer or patch. Prompt quality is one of the most important parts of the framework: engineering the full prompts is a first-class deliverable, not an incidental template-writing task.

## 2. Architecture, interfaces, and persistent records

### Package organization and compatibility

Integrate MCTS into the existing ownership boundaries described in `alpha_agents/tree_search/STRUCTURE_PLAN.md`. The initial implementation collected its components under `bootstrapped/`; that parallel package is an interim implementation, not the intended final structure. Preserve the working algorithm and persistent invariants while relocating each responsibility to its existing owner.

The concrete migration map is:

| Current `bootstrapped/` component | Final owner and location | Reuse or integration requirement |
| --- | --- | --- |
| `contracts`, `policy`, `controller`, `coordinator` | `core/mcts/` | Add MCTS alongside the existing DGM controller; retain DGM interfaces and behavior. |
| `storage`, `artifacts`, `recovery` | `core/mcts/` initially | Reuse existing run locking, atomic record writes, and Git helpers. SQLite state and content-addressed artifacts retain their distinct semantics; avoid replacing legacy JSON archives. Extract shared helpers into existing core modules only when both controllers actually use them. |
| `plan_archive`, `planning_scope`, `value_context`, `finalization`, `reporting`, `assessment_timing`, controller-owned targeted-check admission | `core/mcts/` | Keep proposal diversity, horizon semantics, score identities, reservations, and reporting owned by the controller. Android execution remains behind an injected evaluation interface. |
| `codex`, `codex_worker`, `permissions`, Codex prompt templates and SDK telemetry | Existing `mutators/coding_agents/codex/worker.py`, shared `codex/execution.py`, and `codex/roles/` adapters | Use one worker command entry point for DGM mutation and native MCTS roles. Share SDK goal collection, durable-goal resume, launch monitoring, idle checks, and launcher probes; keep role contracts and response validation in adapters. Store every goal objective and role prompt in external files. Remove the unused `bootstrapped/` package. |
| `responses` | Split at the provider boundary | Put native Plan extraction, JSON parsing, SDK response schema construction, and Codex-specific syntax validation beside the Codex adapter. Keep intervention admission, value bounds, objective identity checks, and reward-rubric validation in `core/mcts/role_contracts.py`. |
| `evaluator`, `runtime_attestation`, `prepare_subset` | `bridges/androidworld/` | Add staged evaluation and deterministic subset preparation to the existing bridge. Continue using the current trusted runner, normalized outcomes, fixture checks, handoff, and cleanup. |
| `scheduler` and task execution/cache adapter | `bridges/androidworld/` | Own device leases, execution, retry, quarantine, and task-result verification here. Access transactional job records through core storage interfaces; do not make the controller import AndroidWorld runtime code. |
| `composition` | Existing `cli.py` plus a focused composition module | Keep DGM/MCTS selection in the established composition layer. Avoid another CLI or a new all-purpose framework package. |

Provider-neutral request/result contracts belong in core; Codex translates them into native Plan/Goal requests and renders its detailed prompts. The controller receives a role gateway and staged evaluator through interfaces. Core must import neither the Codex SDK nor AndroidWorld runtime. Shared process supervision remains in `infrastructure/`. Optional engineering validators retain their existing ownership and stay independent of benchmark success.

Carry out the correction in this order:

1. Inventory existing implementations and extract only genuinely reusable helpers; identify DGM-specific contracts that cannot express MCTS state or multi-role calls.
2. Let active admitted work reach a recoverable boundary before changing worker/module paths. Record process exit, task publication, reservations, and pending trial state. Preserve original run records and exact role requests.
3. Move provider-independent MCTS modules into core, then Codex adapter modules into the existing Codex package, then AndroidWorld modules into the existing bridge. Preserve archived source and requests for historical runs. Retain a forwarding entry point only when a real caller needs it and its request contract is proven compatible; an unused forwarding package is not part of the final design. Keep one implementation of each shared SDK concern.
4. Update imports, worker launch paths, permission source closures, package resources, examples, CLI composition, tests, and documentation. Update the deterministic subset command below to its final bridge location.
5. Verify existing DGM regression behavior, generic import isolation, Codex/bridge boundaries, MCTS lifecycle and crash recovery, and packaged worker/resource discovery. Check that the change adds no second writer for artifacts, patches, or canonical state.
6. Recompute provenance for changed trusted adapter paths/code. Resume only when recorded execution contracts remain compatible. When evaluator identity changes, preserve earlier outcomes as historical evidence and use a fresh live run for acceptance; never relabel or silently reuse incompatible task results or scores.

This is an organization and reuse correction, not a replacement of the search algorithm. Existing live evidence proves only the structure/protocol that actually produced it. The final acceptance stages must exercise the corrected package boundaries as well as the original behavioral requirements.

Extend the existing CLI composition layer with `controller.type`:

- Missing or `dgm`: preserve the current implementation and output.
- `fixed_puct`: construct the new controller and its staged evaluator.

Keep the existing `DGMController`, `SearchConfig`, `HarnessBridge`, and schema-version-one archives unchanged. New runs use a distinct format identified by `controller_type="fixed_puct"` and `schema_version=1`.

Reuse process supervision, Git patch capture, Artemis execution, task normalization, fixture checks, answer handoff, and runtime cleanup. Introduce a staged Artemis adapter instead of routing the new controller through the legacy bridge’s whole-candidate `evaluate()` lifecycle.

### Codex roles and ownership

Codex performs five separate reasoning/engineering roles. The controller owns search decisions and statistics, and the trusted Android evaluator owns measured task outcomes. These are separate components with different authority.

| Codex role | When it runs | Required inputs | Concrete output | Authority |
|---|---|---|---|---|
| Action proposer / diagnostic planner | Once per owned harness expansion, in native Codex Plan mode | Exact harness, Screen/S0 outcomes, relevant trajectories/screenshots, judge findings, a versioned archive of existing plans/attempts, remaining controller capacity | Exactly three distinct detailed executable intervention plans plus archive-review and novelty/diversity records; an incomplete proposal remains pending recovery | Inspect supplied code/evidence and finish planning autonomously; cannot edit harnesses, implement plans, or choose a child |
| Policy estimator | After plans are drafted, and on a versioned prior refresh | Exact harness/evidence snapshot, all candidate frozen plans, contradictory evidence, prior outcomes, continuation policy | Raw nonnegative prior for every plan plus comparative rationale; controller validates, normalizes, floors, and versions priors | Recommend relative search allocation; cannot select an edge or alter measured rewards |
| Implementation engineer | Once per reserved parent-plan implementation attempt | Exact parent artifact, frozen plan, allowed edit scope, failure evidence, approved verification specifications, dependency/runtime context | Candidate edits, implementation explanation mapping plan requirements to changed code, check requests/results, blockers, and completion status; controller captures canonical patch/artifact | Edit only the disposable candidate and run appropriate local checks; device experiments use the trusted coordinator queue |
| Value estimator | At a valid nonempty-path frontier, including maximum search depth, before backup | Exact frontier, current AndroidWorld utility, harness judge score, combined score/weight, evidence, capacity/continuation snapshot, discount factor and continuation-policy identity (no remaining-budget input) | One scalar expected discounted continuing net combined-utility change, with rationale, evidence references, uncertainty, and matching objective/artifact/discount/policy IDs | Predict the specified continuation return; cannot edit code, invent measurements, or update Q |
| Independent harness judge | Code phase after artifact freeze; execution phase after relevant trajectories exist; also run for the root | Exact harness, fixed task goals/outcomes, canonical artifact-specific trajectories and common checks, fixed rubric; exclude incoming plan, parent diff, and implementer explanation from score context | Harness-level robustness/recovery, decision quality, and generalizable-behavior findings and scores, aggregate `J(H)`, evidence references, uncertainty, and unknown dimensions | Independently assess the actual harness; its frozen score contributes to combined utility, but it cannot edit artifacts, override AndroidWorld outcomes, or update statistics |

Each implementation attempt uses a fresh engineering context against the same exact parent and frozen plan. It must not inherit an unrecorded patch or conversation from a sibling attempt. The judge is a separate reviewer job, not the implementer's self-assessment. Proposer and policy are distinct invocations so diagnosis/proposal and comparative allocation are inspectable independently.

The non-Codex components have these responsibilities:

- **Search controller:** PUCT selection, trial/attempt/depth limits, ownership/reservations, artifact publication, score eligibility, backup, and final winner selection.
- **Trusted Android evaluator:** task generation/reset/execution/grading, trustworthy per-task outcomes and trajectories, and measured Screen/S0/full-Selection aggregates.
- **Storage and scheduler:** immutable artifact identity, evidence/version records, device leases, process supervision, cache compatibility, and exactly-once persistence.

No Codex role is the AndroidWorld benchmark evaluator. AndroidWorld utility is measured task evidence; `J(H)` is a separately labeled subjective harness-quality assessment; `F(H)` combines them using the frozen weight. The value scalar predicts future change in `F`, not a current benchmark score. Judge quality labels and confidence are not calibrated success probabilities.

### Role handoffs during a trial

1. For an evaluated harness, the proposer diagnoses failures and drafts concrete intervention plans; the controller publishes their frozen contents after the owned expansion settles.
2. The policy estimator compares those plans and returns priors. The controller versions effective priors and selects a plan using PUCT.
3. The implementation engineer realizes that frozen intervention in a fresh candidate and verifies its intended behavior; it does not choose a replacement intervention.
4. After the controller freezes the child, the harness-code judge phase and trusted Android evaluation run concurrently. The execution judge phase incorporates actual trajectories and publishes the canonical artifact-quality score.
5. A child with complete S0 outcomes and a usable frozen judge score receives a combined score and becomes eligible for continuation; these findings also form the development context for the next expansion. Repeat until the trial reaches its declared frontier.
6. The value estimator predicts expected discounted continuing net improvement under the declared policy at every nonempty-path frontier, including maximum search depth. Only proven structural exhaustion retains the absorbing zero convention. The controller commits discounted reverse backup, `G_t = r_t + gamma * G_(t+1)`.

### Main interfaces

Use typed dataclasses and protocols. Core search imports must remain independent of AndroidWorld and optional Codex SDK packages.

| Interface | Required behavior |
|---|---|
| `PlanProposer.propose(context)` | Return exactly three distinct concrete intervention plans and evidence references for a successful expansion; fewer plans constitute an incomplete assessment and cannot close expansion |
| `PolicyEstimator.assess(context, plans)` | Return a prior for every supplied plan and a rationale |
| `ImplementationBackend.implement(parent, plan, attempt)` | Edit a disposable parent copy according to the frozen plan |
| `StagedEvaluator.submit(artifact, stage_request)` | Schedule explicit task instances and return a persistent evaluation handle |
| `StagedEvaluator.collect(handle)` | Return completed, incomplete, or rejected evidence |
| `ValueEstimator.predict(context, continuation_snapshot, remaining_modifications)` | Predict net utility change under the capacity-constrained continuation, with rationale and uncertainty |
| `ArtifactJudge.start_code_review(context)` | Start an independent harness reviewer job using exact artifact code and the common rubric, without incoming-plan or parent-diff context |
| `ArtifactJudge.finalize_execution_review(handle, context)` | Finalize artifact-level findings and `J(H)` from the canonical execution evidence and common rubric |
| `ArtifactStore` | Freeze, verify, reconstruct, and export exact harness artifacts |
| `SearchStore` | Atomically reserve work, persist lifecycle state, and commit backups |
| `ModelCallBudget` | Reserve top-level Codex invocations and protect admitted trials' required value calls/retries within the configured invocation cap |
| `CostLedger` | Record actual and bounded/estimated resource usage; device-hour accounting does not deny work |

Each component exposes an `identity()` containing its implementation version and configuration. Requests and responses are persisted before their results influence search.

Codex adapters use a common role-request envelope: `run_id`, `role`, `phase` where applicable, artifact/plan/trial IDs, input snapshot IDs, prompt/schema versions, configured model/effort, permission mode, native collaboration mode, plan-archive snapshot ID where applicable, and `timeout_seconds=7200`. Outputs include role-specific payload, completed/partial/failed status, evidence references, blockers, elapsed time, and usage. The controller validates both the common envelope and role-specific schema before publishing a result.

### Authoritative storage

Use SQLite from the Python standard library, with WAL mode, foreign keys, a busy timeout, and short transactions. Every Codex invocation receives a private writable `role_requests/<request_id>/scratch` directory, separate from its source workspace and canonical evidence. Publish its exact path in the input envelope; read-only assessment/planning roles may write derived inspection copies there while source and sealed evidence remain read-only. Copy SQLite DB/WAL/SHM into scratch before querying or creating a standalone backup; cite original sealed evidence, never mutable scratch. Reject path-traversing request IDs and scratch-directory symlinks. Bind these instructions to the prompt-library identity. Hold one process-level run-owner lock; trial workers share the database through separate connections.

The database is authoritative for search state. JSON files are readable exports, not competing state stores.

Store these logical records:

| Record | Essential fields |
|---|---|
| Run | Root ID, configuration fingerprint, search phase, counters, configured shortlist limit, Codex invocation balance/reservations, cost totals, stop reason |
| Harness node | Artifact ID/hash, parent, plan, attempt, depth, eligibility, frozen S0/judge/combined-score snapshot IDs |
| Plan | Parent node, immutable `PLAN.md` hash, intervention fields/key, hypothesis, evidence, archive-review snapshot, closest prior plan IDs, novelty rationale, diversity audit, version |
| Attempt | Trial, parent-plan slot, lifecycle state, implementation request, patch, artifact, error, cost |
| Policy assessment | Node, evidence snapshot, plan IDs, priors, configuration and prompt versions |
| Evaluation | Artifact, stage, task manifest, execution fingerprint, status, aggregate, evidence |
| Task job/result | Task instance, seed, device, attempts, outcome classification, trajectories, timing |
| Judge assessment | Artifact hash, canonical evidence snapshot, rubric/model versions, per-dimension findings/scores/unknowns, aggregate J, status, cost; no scored plan alignment |
| Manual judge narrative review | Original judge snapshot, separately metered validated review invocation, exact reviewed response, manual evidence/provenance, reason; every dimension score and unknown status must match the original |
| Combined score | Artifact, U/J snapshot IDs, objective/version, frozen blend weight, F |
| Value assessment | Frontier, U/J/F snapshots, blend/objective, discount factor, continuation-policy identity, branching-capacity snapshot, continuation version, absorbing-stop rule, scalar, provenance |
| Trial/path | Ordered valid transitions, reward snapshots, selection-time prior versions, reservations, frontier, lifecycle state |
| Node operation | Node, expansion/refresh kind, owner, fencing token, heartbeat, request ID, result snapshot, operation status |
| Statistics | Plan and implementation `N`, `W`, `Q`, plus completed node visits |
| Backup | Unique trial backup ID, ordered returns, observed and predicted contributions |
| Cost entry | Operation, stage, actual/bounded/estimated usage, resource, timestamps; model-call reservations additionally record admission/completion bucket and owner |

Add uniqueness constraints for:

- `(parent_node_id, plan_slot)`;
- `(plan_id, implementation_slot)`;
- `(trial_id, path_position)`;
- `backup.trial_id`;
- task execution/cache identity;
- one active expansion or prior-refresh owner per node.

Persist the exact plan-prior assessment version and eligible-set/uniform-implementation-prior snapshot used by every selection. This is required for replay and for diagnosing the effects of prior refreshes.

Use transactions to allocate IDs, reserve implementation slots and Codex invocations, and persist resource usage. Never hold a database transaction during a model call or device operation.

### Artifact and evidence ownership

A frozen artifact consists of a manifest and exact harness content, including executable permissions and tracked symlink identities. Its hash must cover the complete declared harness—not only Python files.

Store the root snapshot and each child’s binary patch relative to its exact parent. Verify reconstructed content against the child manifest before execution or export.

Preserve Artemis modules currently exposed through the `.dgm_preserved_modules` mechanism. Do not apply a broad `output*` exclusion that can remove required source modules.

Separate:

- disposable implementation workspace;
- frozen artifact;
- evaluator execution workspace;
- development evidence;
- protected Confirmation evidence.

Evaluation must never modify the canonical artifact. Runtime-generated files and dependency links do not become inherited harness content.

Identical generated artifacts still receive separate attempt and node records. Their artifact blobs and compatible task outcomes may be reused, but their lineage, cost, and search statistics remain distinct.

Build explicit evidence snapshots containing file hashes, task IDs, trajectory event references, and assessment versions. Supply development evidence through an allowlist; do not recursively copy the complete run directory into model contexts.

## 3. Search behavior and Codex workflows

### Initialization and expansion

Before launching trials:

1. Validate configuration, task partitions, ports, dependencies, model endpoint, Codex capability, fixture identity, and output location.
2. Generate and verify the frozen S0 manifest using the preparation command.
3. Freeze the configured shortlist limit (default: three non-root candidates) and publish a final-Selection task-count/time forecast for observation only. No device-hour affordability check is performed.
4. Freeze the root artifact, execution contract, combined-objective weight, and harness-quality rubric; register its Screen/S0 evaluation and reserve two root judge invocations. Start root code review in parallel with Android evaluation.
5. Evaluate the root on all five Screen tasks and apply the explicitly configured gate. Under `strict_all_success`, stop with baseline diagnostics if any valid task outcome fails.
6. Evaluate the root on the complete frozen S0, finalize its harness judge review using the same artifact-only rubric as every child, and store immutable U/J/F snapshots. Do not assign an invented root judge score.
7. Publish expected-runtime/device-hour forecasts from root timings separately from timeout-based upper bounds, including reviewer latency. Neither forecast constrains device admission or final evaluation.

Expand a harness only when it is eligible and below depth eight.

The proposer runs in native Codex Plan mode. It receives its exact artifact, available development evidence, a versioned existing-plan/attempt archive, and remaining depth. It must inspect existing interventions before producing new candidates, and finish the planning task without executing a modification. Every proposed plan must include:

- failure evidence and references;
- causal hypothesis;
- target component and concrete intervention;
- scope and implementation constraints;
- predicted observable behavior;
- executable checks or declared diagnostic checks.

### Existing-plan review and rollout diversity

Prepare a versioned plan catalog at expansion reservation time. Include every plan already published in this run, with parent artifact, frozen plan hash, target component, operation, conditions/parameters, scope, intended mechanism, current status, and prior attempt/outcome summaries. Include existing plans at the current harness, ancestor plans, sibling interventions, and the remaining run archive. Supply the complete catalog index; provide readable full frozen plan documents and evidence by ID. Do not silently omit old plans to fit the initial prompt.

Each attempt summary links a hash-verified immutable evidence bundle containing its exact parent/child identity, available benchmark snapshots and trusted trajectory references, full harness-judge findings, score provenance, and explicit unavailable fields. Preserve the bundle itself in SQLite as an immutable snapshot; new attempt states or outcomes produce new versions without changing older role inputs. Rebuild missing bundle files from their original SQLite snapshot before execution. Grant non-judge roles reads to the declared bundle files and trusted task evidence only; paths found inside model-authored judge narratives do not confer filesystem access.

The proposer must first identify which existing interventions already address the current failures, read the closest relevant plan documents and their recorded attempts, and explain what those attempts established or left unresolved. It must then compare the new candidates with each other and with existing interventions. Require an `archive_review` record listing inspected plan IDs, relevant prior outcomes, unresolved evidence, and any catalog/read-access gaps.

Judge novelty by the actual intervention: target, operation, triggering conditions, parameters, scope, and intended executable mechanism. Rephrasing a hypothesis, changing a title, adding a different explanation, or making a cosmetically different patch to the same mechanism is not a diverse action. Each candidate records its closest existing plan IDs, similarities, substantive differences, expected distinct trajectory/code effects, and why the new branch is worth trying. Different mechanisms addressing the same observed failure can be useful diverse alternatives.

Require exactly three distinct supported plans and a pairwise diversity audit. Continue diagnosis and archive investigation until three feasible causal interventions are developed. Do not fill the count with variations of one idea. A proposal that cannot support all three remains an incomplete assessment requiring recovery; it does not close expansion or establish terminal exhaustion.

At the same exact parent, reject a candidate equivalent to an already registered action; further implementation sampling belongs to that existing parent-plan pair. Across different parents, an intervention may legitimately recur because the harness/evidence has changed, but require an explicit adaptation rationale and an explanation of why the prior outcome does not settle its usefulness here. Default to unexplored, evidence-supported interventions over redundant new branches. Do not ban a justified repair solely because a different ancestor used similarly named logic.

Normalize intervention fields for exact duplicate detection at publication. The proposer performs the richer semantic comparison within its owned planning run; v1 does not add a separate embedding service or similarity-model call. The controller validates archive snapshot/plan references and the presence of substantive novelty/diversity records, and rejects exact same-parent duplicate action keys. Semantic novelty remains an evidence-backed proposal judgment, not a claimed calibrated distance.

Persist the catalog snapshot, inspected IDs, candidate comparisons, and accepted/rejected duplicate decisions with expansion provenance. Recheck the archive version before publication; if relevant plans appeared concurrently, provide their records for a bounded diversity recheck within the same two-hour planning invocation. Reserve fresh invocation capacity before any additional run if that deadline has expired; do not publish candidates against an unreviewed relevant archive change.

Register exactly three plans atomically, then freeze them before implementation. Each plan has three implementation slots, filled through MCTS selection rather than a batch of nine candidates. Failed or incomplete proposal calls are administrative failures, not evidence that continuation is impossible. Budget cutoffs and interrupted attempts can leave slots unfilled; reports must show these as pending or unattempted, never as a completed nine-branch expansion.

The policy estimator scores the registered plans. Validate finite, nonnegative priors with a positive total, normalize to `p`, then apply `p_effective(a) = max(p(a), epsilon) / sum(max(p(j), epsilon))`, with experimental default `epsilon = 0.01`. Apply the floor to every refresh. An all-zero assessment is invalid and follows the assessment retry policy. Record raw, normalized, floored priors and the floor parameter.

The floor prevents an eligible zero-prior plan from receiving a permanently zero exploration bonus. It does not promise every plan an attempt within 30 trials. Budget and eligibility still constrain exploration.

Refresh priors only when a newer development evidence snapshot exists and the harness is selected again. Keep plan contents frozen, version each refresh, and preserve existing `N/W/Q`. A proposed material plan revision is a new action and can be admitted only while an expansion is still open and within the three-plan limit. Successful initial expansion closes plan admission only after all three feasible distinct plans are registered. Further material revisions are recorded as deferred hypotheses, not substituted into frozen actions or used to reopen capacity.

### Expansion and selection concurrency protocol

Before any proposal or initial policy call, transactionally reserve node expansion ownership. The reservation identifies the node, owning worker, unique operation ID, and monotonically increasing fencing token. It has a 30-second heartbeat and a 120-second stale threshold. One owner performs proposal and initial policy assessment outside the transaction, persisting intermediate requests/results. It then atomically publishes frozen plans, their first prior version, and the expansion status only if it still owns the fencing token.

Because proposal diversity depends on the complete shared plan archive, admit only one archive-sensitive expansion at a time across nodes. Acquire this reservation atomically with node ownership and retain it through proposal, initial policy assessment, and publication. Other workers may continue implementation, evaluation, stored-edge traversal, and prior refresh; a worker needing another expansion waits without spending model calls. Release the archive reservation on publication, joined failure, or verified stale-owner reclamation through the same persisted operation and fencing token. Retain the publication-time catalog check and bounded recheck as defense against stale inputs and older writers.

A second worker encountering an expansion in progress waits on the coordinator's completion event and rechecks the database; it does not issue another proposal or policy call. Zero-, one-, and two-plan responses are incomplete and leave expansion open for recovery. An assessment error never closes expansion as infeasible.

Prior refresh uses the same ownership record and fencing protocol. Serialize refresh with plan-set changes. Workers needing that evidence version wait for the refresh to settle; no worker launches a redundant refresh for the same node/evidence snapshot.

Publish the full validated policy comparison, uncertainty, and evidence together with each raw/normalized/effective prior version in the same transaction. Bind the assessment to its artifact, frozen plan IDs, evidence snapshot, and original invocation/model/effort/prompt provenance; retain a content hash. Cached responses retain their original invocation provenance. Frozen value evidence includes the full assessment for its captured prior version, so later refreshes cannot rewrite the reasoning behind an earlier selection or prediction. Older numeric-only records explicitly lack this reasoning; do not reconstruct it from a newer assessment.

Reclaim ownership after a stale heartbeat only when the coordinator verifies that the recorded operation/process is no longer live, cancels and joins it if necessary, and advances the fencing token. On restart, first reclaim or terminate previous-owner processes. A late response carrying an old fencing token cannot publish plans or priors. Reuse already persisted valid responses when recovering the same operation.

For selection, use one short transaction to read completed statistics and the exact prior version, apply reservation-aware eligibility, choose the edge, recheck trial/global/local attempt limits and Codex invocation availability, and reserve the path and implementation slot. If limits, invocation availability, or ownership changed, roll back and reselect. Release path reservations in the trial's backup, failure, or cancellation transaction. File uniqueness constraints alone are not the concurrency protocol.

### Selection and eligibility

Use the specification’s plan-level PUCT equation with completed statistics.

At zero node visits, choose the highest-prior eligible plan. Break ties by stable plan ID.

A plan is eligible when it has either:

- an available implementation slot and sufficient unreserved Codex invocations to admit the implementation/review work; or
- an eligible stored implementation.

Prefer unreserved eligible branches. If all useful branches are currently reserved, wait for a completion event and reselect. Pending counts do not alter measured returns or completed visits.

For a selected plan:

- Generate one implementation if capacity and the trial’s remaining attempt allowance permit it.
- Otherwise choose an eligible stored implementation using implementation-level PUCT.
- Initialize implementation priors uniformly over the currently eligible children.
- Break implementation ties by stable implementation slot ID.

Do not generate all implementations eagerly. Do not rerun Codex when traversing an existing child.

### Trial lifecycle

Each worker follows this sequence:

1. Atomically reserve a trial ID within the 30-trial limit together with two protected value-assessment invocations (initial call and permitted retry).
2. Start at the fixed root with an empty path and zero new attempts.
3. Select a plan and either reserve a new attempt or select a stored child.
4. For a new attempt, reconstruct the exact parent and supply the frozen plan.
5. Capture and freeze the resulting artifact and patch; start the harness-code judge phase using artifact-only scoring context.
6. Run Screen and applicable targeted checks while code review executes independently.
7. If the declared Screen gate passes, run every S0 task and persist trajectory evidence as it arrives.
8. Start the execution-grounded judge phase when the required S0 evidence exists, passing the earlier code findings and evidence references; wait only within its declared deadline.
9. Register the child as eligible only after complete valid S0 evidence and a usable frozen harness judge assessment exist; persist U/J/F snapshots.
10. Append the transition using the cached combined-score difference, with the AndroidWorld and judge contribution recorded separately.
11. Continue from that exact child.

End the trial at depth eight, after three new attempts, at a computational budget cutoff, after an invalid attempt, or when continuation is genuinely exhausted.

Stored transitions consume depth but no new-attempt allowance. Invalid attempts consume both the trial allowance and the parent-plan slot. Negative utility change does not end a trial.

Validate all limits before reserving new work. There must be no leaked attempt reservation when trial limits or the Codex invocation cap prevent admitting another implementation.

### Implementation validity and failure behavior

A permitted, nonempty patch may be evaluated even if Codex times out or reports failure; retain that implementation status in evidence.

Reject protected-file edits, missing/corrupt artifacts, and empty patches as invalid attempts. Preserve available logs and diffs without inventing a utility score.

Apply the separately declared Screen success gate only to valid benchmark outcomes. Under optional `strict_all_success`, one ordinary Screen failure rejects the child. Under the current live runs' `execution_valid` setting, valid failures are diagnostics and the child proceeds to S0. Incomplete infrastructure evaluation keeps the child ineligible under either policy; it is not a measured task failure.

After an invalid attempt:

- end that trial’s continuation;
- use the last valid frontier;
- bootstrap if continuation from that frontier remains possible;
- back up only valid traversed transitions;
- record an empty failed trial without a Q update if no valid transition exists.

Invalid attempts are not assigned a synthetic negative reward. Report their rate and cost separately.

A valid node is terminal for actual search at depth eight, or when a persisted controller-capacity snapshot proves there is no permitted outgoing harness transition. Only the latter is structural exhaustion for the independent forecast; max depth alone is not absorbing for value estimation. For exhaustion, expansion must be closed; there must be no admissible new plan/implementation slot and no eligible stored implementation to traverse. An exhausted generation cap alone is insufficient: existing scored children remain valid transitions, even when no additional generation is possible. Pending implementations, incomplete evaluations, and in-progress expansion/refreshes require waiting or recovery; they are not evidence of exhaustion.

At a genuinely exhausted frontier, the continuation holds utility constant thereafter. Reaching the search depth cap alone still requires a value estimate under counterfactual depth allowance beyond that cap. Zero therefore means that this fixed controller permits no further utility change from that frontier, not that the harness has no improvement potential. Budget exhaustion, an operator stop, and assessment outages are computational stops and retain the bootstrap target.

### Combined harness score and reward

Preserve the AndroidWorld utility `U_S0(H) = sum(w_j * y_j(H))` on the frozen subset. Independently score harness quality `J(H)` on the common rubric below. For the configured blend:

```text
F(H) = (1-lambda) * U_S0(H) + lambda * J(H)
r(H -> H_child) = F(H_child) - F(H)
                = (1-lambda) * delta_U + lambda * delta_J
```

The experimental weight for review is `lambda=0.10`; it is configurable and frozen in the objective identity. `lambda=0` is the AndroidWorld-only comparison controller. Both U and J lie in [0,1], so F lies in [0,1] and undiscounted immediate rewards telescope to `F(frontier)-F(parent)`. Discounted returns instead apply `G_t = r_t + gamma * G_(t+1)`; they do not telescope to final score change. Do not add raw child judge scores per transition: repeatedly generating a high-rated harness without an improvement must not accumulate judge bonuses.

Example: parent `U=0.60, J=0.40` has `F=0.58`; child `U=0.70, J=0.80` has `F=0.71`. Its reward is `0.13`, comprising `0.09` AndroidWorld improvement and `0.04` judge improvement. If U and J do not change, reward is zero.

Keep the two components visibly separate. Combined search prioritizes harness quality as well as task success, so it can allocate differently from AndroidWorld-only search. It does not relabel a subjective judge assessment as a benchmark measurement. Final full-Selection winner choice remains the independently specified AndroidWorld ranking rule.

Require the same objective/rubric/weight for parent and child, and verify that each node's U/J/F snapshot IDs correspond to its own exact artifact and canonical evidence. Freeze the first usable J assessment for each artifact/evidence/configuration key during search. Repeated visits and identical artifacts with compatible canonical evidence reuse it; neither a new incoming plan nor a different implementer explanation changes the scoring key. Extra judge reassessments remain separate evidence until an explicit objective/statistics-version rebuild is requested.

### Allocation consistency and descriptive forecast review

Supply proposer and policy roles with the same frozen discount factor and
continuation-policy identity/definition used by value. Compare expected
discounted benefits, their arrival order, intermediate regressions and
uncertainty. Keep judge scoring contexts artifact-only. Actual discount steps
count harness transitions, not failed calls, tokens or wall-clock duration.
Discounted differences can assign positive return to temporary gains followed
by reversals; this allocation preference is separate from best artifact quality.

Retain current PUCT coefficients until matched-budget experiments justify a
change. Report `value_calibration` separately from measured retained performance:
compare frozen forecasts with subsequent completed trial suffixes under matching
policy/discount. Persist a traversal-row cutoff with each snapshot. For K observed
transitions, retain the unknown tail bounds `gamma^K * [-F(H_K),1-F(H_K)]`; only
genuine exhaustion gives a complete return. Never use a later prediction as an
observed tail. These samples are adaptively selected and may reuse correlated
evidence; prefix intervals and point differences are descriptive diagnostics,
not independent statistical calibration. Historical snapshots without a cutoff
are unavailable. A change to exhaustion/reopening rules versions the policy.

### Frontier values and backup

Request a value only after the trial's evidence and continuation snapshots are frozen. Include the exact frontier artifact, current U/J/F snapshots, S0 identity and weights, judge rubric/version and scoring protocol/model, blend weight, available evidence, continuation procedure/version, and the run-frozen `discount_factor` and `continuation_policy_id`. Future J is assessed under that same frozen protocol; the value rationale must acknowledge uncertainty in both future task outcomes and future judge ratings.

The continuation snapshot must include registered/closed plan status, unused plan capacity, per-plan consumed and remaining implementation slots, invalid and pending attempts, eligible stored children and their identities/depths, current priors/statistics, and the controller's generation-versus-descent rules. Snapshot the relevant reachable stored subtree and provide references to its exact records. Record snapshot-time pending work; do not reinterpret a later completion as evidence available to the prediction.

Define the target as `V_pi(H, E, C) = E_pi[sum(t>=0) gamma^t * (F(H_(t+1)) - F(H_t)) | H,E,C,objective]`, where `0 < gamma < 1` and `C` is the frozen capacity/evidence snapshot. There is no explicit remaining-budget input. Genuine termination holds utility constant thereafter. This is expected discounted improvement under the specified policy, not unrestricted optimal harness quality. The default `discount_factor=0.9` is an experimental starting point. Since `V = -F(H) + (1-gamma) E[sum(k>=1) gamma^(k-1) F(H_k)]`, the existing bounds remain valid.

Freeze reachable-node evidence in the same SQLite transaction as capacity: canonical outcomes and full judge findings, available development evaluations, complete registered interventions, exact prior distributions, and previous value findings with their original capacity identities and summaries. Export the full immutable snapshot as a hash-verified readable continuation bundle, accompanied by read-only exact artifact code views. The value role may inspect only the declared views and trusted evidence references; judges do not receive those archive/code-view grants. Recovery reuses the original evidence cutoff and can rebuild missing bundle files from SQLite. Earlier capacity summaries omit nested evidence to prevent recursive duplication of assessment history.

At a computational cutoff, estimate this continuation under a counterfactual availability of computation sufficient to continue the same controller. Do not force value to zero merely because the current run has exhausted discretionary budget. Used local branching capacity is preserved, eligible stored children remain traversable, and future nodes receive their own local capacity. New plans and implementations receive canonical evaluation/judging; completed evidence refreshes priors and discounted statistics. Actual depth/global budget limits do not truncate hypothetical continuation. Persist measured and predicted contributions and the capacity/continuation versions so historical returns retain their original interpretation.

Validate:

- returned artifact, discount factor, continuation-policy and snapshot identities;
- finite scalar;
- `−F(H) ≤ value ≤ 1−F(H)`;
- permit a bounded nonzero prediction at maximum search depth; zero is required only for genuine structural exhaustion.

Retry a failed or invalid assessment once using the trial's protected completion reserve. If it still fails, mark the trial `awaiting_value`, stop admitting new trials, and let already running work reach durable checkpoints. Preserve the unbacked-up path and its original evidence/capacity snapshots. Never substitute zero for an unavailable prediction.

On resume, reserve a new pair of assessment invocations before retrying that pending value; reuse a valid response already persisted before interruption. If the Codex invocation cap has no remaining capacity, report `recovery_budget_required`. An explicit operator increase may raise that invocation cap with an audited ledger entry; root, task objective, branching/depth/trial limits, and assessment protocol stay unchanged. Recovery does not regenerate implementations or reevaluate completed tasks. Protecting invocations permits the bounded assessment attempts; it cannot guarantee a model response or working infrastructure. Device-hour totals never require a recovery-budget grant.

For a nonempty path:

```text
G = frontier_value

for transition in reverse(path):
    G = transition.reward + G
    update the selected plan edge with G
    update the selected implementation with the same G
    increment the transition's parent node visit once
```

Commit all statistics, the backup record, trial completion, and reservation release in one transaction.

The unique backup ID makes retries idempotent. Store the measured AndroidWorld contribution, assessed judge contribution, and predicted combined-utility contribution separately. Cached traversal contributes a new continuation return but does not increase benchmark sample counts.

### Codex role execution

Use fresh contexts for proposer, policy, value, each artifact reviewer job (root included), and each implementation attempt. The two phases of a child reviewer job are linked through its persisted code assessment and explicit evidence references, independently of the implementer. Sharing the configured model does not imply sharing an engineering conversation.

Retain the existing SDK-backed engineering execution and recovery machinery for implementation, but supply a dedicated frozen-plan prompt. Remove legacy instructions that the mutator independently chooses an easiest-first task, chooses/revises the intervention, or directly runs an unaccounted device experiment. The selected frozen plan determines the engineering target and may address a reusable mechanism spanning multiple task failures. The implementer can refine details and repair its own implementation within that scope; it cannot rewrite the frozen action.

Policy, value, and judge assessment roles use `codex exec` with explicit read-only execution, JSONL telemetry, and a JSON output schema. These capabilities are documented in [OpenAI's non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode). The proposer instead uses the SDK/app-server planning adapter described below to enforce native Plan mode and preserve its full proposed-plan output.

### Native Codex `/plan` mode for the proposer

Treat `/plan` as an actual collaboration mode. Initialize a planning thread through the Codex app-server adapter, verify the available Plan preset using `collaborationMode/list`, and start proposal turns with the native mode payload:

```json
{
  "collaborationMode": {
    "mode": "plan",
    "settings": {
      "model": "<configured Codex model>",
      "reasoning_effort": "<configured effort>",
      "developer_instructions": null
    }
  }
}
```

Use `developer_instructions=null` to retain built-in mode instructions; supply the detailed role objective, archive, evidence, and completion/output requirements as the task content. Use a read-only artifact/evidence view and prevent implementation side effects. If the Python SDK does not expose the experimental mode fields, use the app-server protocol adapter directly rather than converting the proposer into an ordinary execution run.

The installed protocol schema was inspected and exposes `plan`/`default` mode kinds and this `turn/start` field. Official [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server) describes the mode behavior and completed plan items. Preflight must verify native-mode availability for the configured runtime; unsupported mode is an actionable compatibility failure, never a silent default-mode fallback.

Capture the final completed native `plan` item as the authoritative proposal document. Do not force it into the ordinary assessment JSON-only response or truncate it to a short summary. Require stable document sections for archive review, each candidate plan, and the diversity audit; the adapter deterministically extracts candidate sections and validates their required metadata against the proposal contract. The controller writes each exact accepted candidate section to its frozen `PLAN.md`; the proposer does not write or edit source files. Store the full native plan document and mode/request/event metadata for provenance.

Plan-mode autonomy means completing investigation, comparison, and detailed proposal autonomously within two hours. It never authorizes implementing the plan. Resolve routine planning choices from the run manifest and evidence; record noncritical assumptions. If a required fact is missing, report a specific blocked requirement rather than silently guessing a benchmark/objective change or waiting on an unnecessary approval request.

Persist prompt text, schema, input snapshot, model/effort, CLI/SDK version, raw events, parsed response, elapsed time, and reported usage.

### Two-hour execution contract for every Codex run

Set the shared run timeout to 7,200 seconds for every role invocation. Do not silently shorten assessment or reviewer deadlines. Each run can make multiple tool calls and engineering/reasoning iterations within that allowance. It may finish earlier when its role-specific completion criteria are satisfied; do not force idle waiting or repetitive work merely to consume two hours.

| Role invocation | Maximum wall time | Retry/recovery behavior |
|---|---|---|
| Proposal | Two hours | One separately metered retry; then stop admission for recovery |
| Policy | Two hours | One separately metered retry; then stop admission for recovery |
| Implementation | Two hours | Preserve partial patch/status on failure; no automatic new implementation attempt |
| Value | Two hours | One separately metered retry using the protected pair; then `awaiting_value` |
| Judge: harness-code phase | Two hours | No automatic retry; preserve partial/unavailable findings |
| Judge: execution phase | Two hours | No automatic retry; preserve partial/unavailable findings |

The two judge phases are separate Codex runs, so a child may use up to four hours of reviewer execution overall. A permitted proposer/policy/value retry also receives its own two-hour deadline and consumes another reserved invocation. This is not a two-hour aggregate limit for an entire trial or child lifecycle.

Measure the deadline from actual subprocess/SDK operation launch; queued work before launch does not consume the allowance. Tool calls and any waits for requested checks during a live invocation do consume it. SDK transport recovery or an app-server restart inside the same logical invocation inherits the remaining deadline rather than restarting the clock. Persist start/deadline/remaining-time metadata for interruption recovery. Keep task execution and the underlying GUI-model call timeouts separate from this Codex-run limit.

At the deadline, cancel/join the owned process or goal, preserve current logs and partial artifacts/assessments, and apply the role-specific failure rule. Do not label a timed-out run complete simply because it produced parseable JSON or a nonempty patch. The separately trusted evaluator may still evaluate a permitted partial implementation as specified above.

### Native Goal execution for the implementer

The implementer must use native Codex `/goal`, implemented through the SDK/app-server persisted `thread/goal/set` workflow. An ordinary turn containing the text `/goal`, a prompt-only persistence instruction, or a compatibility fallback to one engineering turn does not meet this requirement. Preflight verifies both native Goal and Plan capability before admission.

Each reserved implementation attempt starts a distinct persisted thread and goal for its exact parent artifact and frozen plan. Persist thread ID, goal objective/hash, full rendered prompt and context, goal lifecycle/usage, physical turn IDs, and any transport recovery. Supply the full sophisticated engineering instructions; if the native objective field's length limit requires an external prompt file, retain the complete detailed prompt in an explicitly readable request-context file and use a developed goal objective that points to it and states concrete implementation/verification completion criteria. Do not shorten the role prompt to fit the objective field.

Internal Goal continuation turns belong to the same metered implementation invocation. They must preserve the original 7,200-second deadline. Within-run transport recovery resumes the same thread and stored objective without clearing the goal, resetting its accounting, regenerating the intervention, or restarting the two-hour clock. At timeout, pause/interrupt the native goal, cancel and join its processes, and retain partial code and verification evidence. A blocked, paused, or budget-limited goal is not completed engineering; a permitted partial patch may still be evaluated with its actual implementation status preserved. Goal execution grants no additional benchmark, device, publication, or edit authority.

After admitting a targeted diagnostic and freezing its intermediate artifact, publish an invocation-bound `pending` receipt before waiting on the Android evaluator. Its null snapshot and outcomes are not measurements. A native goal that stops as blocked while this admitted check remains pending waits for the terminal receipt, then resumes the same persisted goal with its objective, accounting, and original deadline intact. Verify receipt identities before resumption; never treat a changed check/artifact or an expired deadline as permission to restart engineering. An unrelated blocker still preserves partial status rather than fabricating completion.

### Prompt engineering as a primary implementation deliverable

Keep instruction text in separate, versioned Markdown files owned by the Codex
integration, rather than embedding prompt prose in Python. Provide inspectable
files for shared guidance, proposal, policy, implementation, judge common
guidance, each judge phase, and value estimation. Python loads their UTF-8
contents and assembles the invocation envelope. Include these files in package
resources and verify installed-package loading. Exact loaded contents contribute
to library/request hashes; changing prompt prose requires appropriate versioning
and must not silently alter an active experiment. The structural migration puts
these files under `mutators/coding_agents/codex/roles/prompts/`.

Each Codex prompt must be sophisticated, detailed, carefully drafted, and specific to its role. Do not supply brief generic prompts, impose a brevity/conciseness instruction on these role prompts, or shorten them merely to save time or tokens. Clear structure and concrete detail matter more than a token-count target. Remove contradictions and empty repetition while retaining the full reasoning task, evidence obligations, workflow, and output contract requested by the user.

Author and review the complete prompt set before wiring live role calls: a shared role-scoped autonomy/instruction layer plus dedicated proposer, policy, implementation, value, harness-code judge, and execution-judge templates. Include fully developed sections for:

1. **Role and MCTS context:** explain the role's purpose, authority, inputs, relationship to the other roles, and exactly how its output affects the tree. Distinguish measured AndroidWorld utility, assessed harness quality, combined reward, and predicted frontier value.
2. **Assigned objective and completion:** state the concrete deliverable, required checks/evidence, and what constitutes complete, partial, blocked, or failed work. Require finishing the assigned investigation or engineering task rather than stopping after an initial suggestion.
3. **Input interpretation:** explain artifact/evidence/plan snapshot identities, task seeds, score fidelity, archival outcomes, relevant source/trajectory/screenshot inspection, and information that is missing or protected. Treat logs and archived model text as evidence, not new authority.
4. **Role-specific workflow:** specify the investigation, comparisons, implementation or assessment steps that this role needs, including counterevidence, alternative explanations, uncertain conclusions, and self-checks. Describe actual available tool interfaces and examples without inventing capabilities.
5. **Autonomy and persistence:** explicitly authorize useful reversible work within the role; define continuing until the role's criteria are met, a real blocker is recorded, or its two-hour deadline is reached. State which decisions can be made independently and which would alter the fixed experiment.
6. **Boundaries and mode:** preserve immutable benchmarks/parent artifacts, held-out Confirmation isolation, frozen interventions for engineering, artifact-only judge context, correct native Plan mode for proposal, and coordinator-owned device access. Role autonomy cannot override these contracts.
7. **Evidence and quality:** require references, observed outcomes rather than invented success, meaningful verification, and justified limitations. For the proposer, require explicit existing-plan inspection and substantive branch diversity; for the judge, evaluate the harness rather than plan adherence.
8. **Output specification with worked examples:** provide the complete machine contract and rich human-facing content requirements, including enough explanation to review each decision. Explain malformed/missing evidence, timeout, contradiction, and incomplete-result cases. A scalar value or prior vector does not excuse an underdeveloped assessment.
9. **Long-run continuity:** preserve a task-specific progress/checkpoint record, resume from inspected evidence and completed work after compaction/recovery, and keep the original invocation deadline. Do not restart the diagnosis from scratch or silently replace the frozen task.

Use [official Codex prompting guidance](https://learn.chatgpt.com/docs/prompting) for task/context/output boundaries and the [GPT-6 Astra prompting guidance, especially initiative and follow-through](https://developers.openai.com/api/docs/guides/latest-model#prompting-best-practices) for role-scoped autonomy. Adapt guidance to this framework without changing the configured model or copying unrelated permissions. The user's requirement for detailed prompts takes precedence over a generic preference for shorter wording.

Include the following shared autonomy content, expanded with the concrete permissions and completion conditions of each role:

> Work toward the assigned role objective using the supplied context. Make routine decisions independently and record assumptions. Continue the authorized investigation, engineering, or assessment until its completion conditions are satisfied; an initial plausible answer is not sufficient. Use permitted inspection, checks, and correction without repeatedly asking for approval. Preserve findings across interruptions. Record a real blocker or deadline when work cannot finish. The proposer completes planning without edits; the implementer completes its frozen intervention; assessment roles complete evidence-backed judgments without altering artifacts or measurements.

Audit inherited instructions, templates, and repository guidance for conflicts before live runs. In particular, remove default brief-answer instructions, legacy easiest-first intervention selection, premature review/approval stops, and any instruction that grants assessment roles implementation authority. Preserve relevant safe-work boundaries. Do not copy an autonomous engineering instruction into Plan mode without adapting its definition of completion.

Version prompts as source-controlled research assets. Persist exact rendered text, component hashes, template/schema versions, model/effort, native mode, and evidence references for every run. Test prompt rendering and instruction interactions and run representative role smoke evaluations; review the quality of outputs manually before the 30-trial experiment. Prompt variants require explicit experiment identities so comparisons do not silently mix instructions.

### Quality-oriented role completion criteria

Give every role a dedicated versioned prompt and schema containing its objective, input identities, edit/inspection authority, evidence requirements, completion criteria, and stop rules. Preserve the explicitly configured model/effort across roles; do not introduce an automatic cheaper-model or lower-effort fallback to reduce latency.

- **Proposer:** use native Plan mode, inspect concrete failures/code/trajectories, review existing plan documents and prior attempts, and finish detailed candidate plans. Connect evidence, hypothesis, intervention, predicted activation, and checks. Include important alternative explanations, contradictory evidence, regression risks, and unresolved assumptions. Demonstrate novelty against the closest existing interventions and diversity among candidates. Develop exactly three supported, distinct plans; continue investigation instead of padding with weak, paraphrased, similar, or redundant interventions. An incomplete set requires recovery and does not establish exhaustion.
- **Policy:** compare all supplied plans on causal support, potential impact, feasibility, regression risk, transfer, and prior attempt evidence. Explain relative priorities and uncertainties; do not describe priors as measured success rates or calibrated causal probabilities.
- **Implementer:** inspect the frozen plan and parent evidence, implement the intended mechanism, run meaningful checks appropriate to the change, inspect the actual outcomes, and fix introduced failures before finishing. Request representative mechanism/trajectory checks through the coordinator when they are applicable. Continue diagnosis/repair/verification while supported problems within the intervention remain and time is available; the first plausible patch is not sufficient completion. Record unrun checks and blockers rather than inventing success. If the hypothesis is contradicted, report that fact and the implementation status instead of switching to another intervention.
- **Value:** assess the exact continuation target using current utility, supported remaining interventions, attempted outcomes, preserved local capacity, and discount factor. Record a plausible path to improvement or regression and why its expected net effect supports the scalar. Distinguish evidence-backed opportunities from speculative ones; do not return the current utility, a best-case maximum, or an unsupported optimism bonus.
- **Judge:** assess the actual harness code and behavior using the common artifact-quality rubric. Inspect relevant tests and trajectories, identify recovery/decision weaknesses, and distinguish reusable mechanisms from task-specific assumptions. Judge demonstrated behavior and robustness, not whether the implementer followed a plan or narrated a convincing change. Do not score plan alignment, incoming action, parent comparison, or implementer explanation. Allow explicitly insufficient evidence and distinguish code-level hypotheses from observed behavior.

The implementer also inventories the parent workspace before verification and audits new or changed files afterward. Test-created logs and databases remain part of artifact identity unless they are already covered by the fixed exclusions. Preserve their diagnostic evidence outside the final artifact, remove only outputs proven to have been created by verification and absent from the parent, and restore any verification-modified protected files from exact pre-check contents. Git metadata is not required for this audit. Never delete original assets or broaden the frozen scope. Scope rejection reports the offending paths so later reasoning can distinguish generated-output contamination from an unauthorized source change.

Controller validation checks structure, identity, numeric consistency, and verifiable references. It does not pretend that schema validity proves a causal hypothesis or guarantees a good patch. Poor mechanism support informs subsequent proposal/policy/value contexts; it does not fabricate a benchmark failure or silently override measured scores.

Proposal or policy failure after retry also stops admission for recovery. Do not substitute fabricated plans or uniform priors for an assessment outage.

Start harness-code review immediately after artifact freeze (root included), in parallel with Android evaluation. It assesses artifact-level behavior and risks under the same rubric for all harnesses. It must label unobserved recovery/decision mechanisms as unverified and publishes preliminary findings, not an additional reward. Persist trajectories as they arrive; final execution review cannot claim observations before those records exist. The final phase incorporates code findings and canonical artifact-specific S0 trajectories/common-check evidence, then publishes the single usable J snapshot.

Use at most two read-only Codex invocations per child, reserved before implementation begins. Each phase receives the full two-hour run limit. Waiting between the code phase and execution phase for Android evidence does not consume either run clock. If S0 finishes while code review is still running, complete or time out that phase within its own allowance before launching the linked execution review; persist intermediate results without inventing a shorter code-phase deadline. For invalid or Screen-rejected children, retain available code findings and skip execution review unless actual execution evidence supports it. Release unused review reservations only after that decision is persisted.

### Versioned domain-neutral role rubrics

The v22 role prompt library integrates proposer-v1, policy-v1, implementer-v1,
code-review-v1, execution-review-v1 and value-v1 gates/assessment procedures.
Plan repair also checks proposer gates and the complete corrected handoff.
Static instructions and JSON guidance contain no application/platform/task-family
assumptions or named source/trace layout. Supplied artifacts and adapter contracts
establish semantics. Capability-specific inspection applies only when supplied.

The common quality rubric is artifact-quality-rubric-v2: fifteen dimension-specific
behavioral anchors replace a single vague shared scale. Confidence measures support
without changing score weights. Mixed observed quality differs from an unknown
dimension; preserve explicit nulls and existing neutral aggregation. Code findings
are hypotheses, not additional votes. Policy priors and discounted value remain
their own quantities, not arbitrary checklist scores. Bind rubric text/version and
prompt/library hashes into run identity. Software verification establishes prompt
integration and contract integrity, not empirical inter-rater agreement.

### Harness-only judge rubric and canonical evidence

Use three equally weighted dimensions for the root and every child:

| Dimension | Artifact-level question | Examples of relevant evidence |
|---|---|---|
| Robustness and recovery | Does the harness detect failures, handle unexpected state, recover or terminate safely, and avoid silent errors or damaging loops? | Control flow/error handling and observed retries, exception paths, loops, recovery and termination events |
| Decision and state-grounding quality | Are actions and completion decisions grounded in the current observed state, with appropriate verification and memory/state handling? | Source mechanisms plus observation records, operation/state transitions, verification behavior, and contradictions between observations and decisions |
| Generalizable behavior | Does the harness implement reusable behavior rather than case-identity, undeclared context-specific constants, or memorized-sequence shortcuts? | Conditions/prompts/tools and behavior across the available canonical S0 tasks; explicitly distinguish code plausibility from demonstrated transfer |

Plan alignment, adherence to a proposed repair, patch size, and implementer effort are not judge dimensions. Do not reward a harness simply because it implements a plausible-looking mechanism or succeeds on the same tasks already counted by U. Use independently supplied canonical outcomes to locate behavior for inspection; J scores the evidenced quality of that behavior, not a second task-completion rate. Broad generalization beyond supplied tasks remains unverified.

Each dimension returns an ordinal score in `{0, 0.25, 0.5, 0.75, 1}` plus evidence references, rationale, and qualitative confidence: `0` means pervasive severe weaknesses, `0.25` substantial weaknesses, `0.5` supported mixed quality, `0.75` consistently sound behavior in available evidence, and `1` strong behavior with no identified material weaknesses in the reviewed evidence. Scores are subjective rubric ratings, not probabilities. A dimension with genuinely insufficient evidence returns `score=null` with a reason; the controller applies a fixed neutral contribution of `0.5` and records the unknown explicitly. This does not claim the missing behavior was observed. The aggregate is `J=(j_robustness+j_decisions+j_generalizable)/3` after that declared neutral convention. Keep unknown counts and coverage visible beside J.

Use only exact artifact code, fixed S0 goals/outcomes, canonical S0 trajectories, and common check evidence in the reward-scoring context. Exclude the incoming frozen plan, parent/diff, previous judge scalar, implementer narrative, and task-specific extra-check selection from the score prompt. Plan-specific targeted evidence may inform diagnosis outside this scalar but does not give a child more reward opportunities than the root. Apply the same deterministic coverage policy to every artifact: supply every S0 outcome summary, then order failed-task trajectories by task ID followed by successful-task trajectories by task ID within the recorded evidence-bundle limits. Report tasks/events/images not inspected. Version prompt, rubric, coverage policy, model/effort, canonical evidence, and score convention together.

The initial code phase may produce preliminary ratings, but only a validated completed execution-phase response publishes J. Its dimension scores may be explicitly unknown under the declared convention. A failed, malformed, or timed-out final response does not become an all-neutral score merely to continue search.

Record code-phase overlap, execution-phase duration, and added continuation latency. Subsequent evidence-consuming Codex calls wait for the declared reviewer phases to complete or reach their two-hour deadlines. A valid final assessment may incorporate a partial/unavailable code phase, but it must still provide the full rubric response. An unavailable final J blocks combined-score publication as described below. Runtime forecasts must include two-hour assessments and the possible additional two-hour execution review on the continuation path, rather than assuming the previous five-/ten-minute reviews. The two-phase workflow is the v1 default; a one-call post-S0 simplification would be a separately configured experiment, not a silent scheduling change.

Judge failure preserves unavailable/partial findings without invalidating AndroidWorld outcomes. If the final J is unavailable, mark the artifact/trial `awaiting_judge`, stop new admission, and preserve its completed Android evidence. Resume explicitly reserves one new final-review invocation before retrying the same canonical evidence; no automatic extra run is hidden outside the invocation cap. Do not back up an AndroidWorld-only substitute, fabricate J=0.5 for an outage, or later add a second judge backup. A usable final J enters F before the trial's single reverse backup. The root must have a usable J before search launches.

## 4. Evaluation, scheduling, resource accounting, and final output

If manual review finds errors in a completed judge's prose, preserve its original
snapshot and scoring history. A separately metered execution review on the exact
same artifact, canonical S0 evidence, and rubric may supply corrected narrative
only after explicit operator review. Store that adoption immutably in SQLite,
including both response identities and manual provenance. Every dimension score
and unknown status must match the original; a changed rating requires a new
experiment rather than a narrative repair. Downstream node evidence, archive
bundles, and reports identify the reviewed narrative separately from the original
score snapshot. Refresh material evidence fingerprints when a narrative review
is adopted. This action changes no reward, utility, visits, backups, or invocation
accounting and does not automatically admit search work.

For downstream role references, derive an explicit canonical task index from
existing S0 evidence bundles under the run's owned `tasks/<task-id>/<execution>/evidence`
directories. Include each exact task ID and evidence path in the invocation
context. Reject missing, foreign, or symlinked bundles from this index; an ID
appearing only inside a path is not automatically an accepted symbolic citation.
The index describes existing canonical evidence and never changes stored outcomes
or grants access to additional files.

### Frozen S0 proposal

The current Selection manifest has 20 tasks across five family/difficulty strata. Choose ten using these quotas:

| Stratum | Full count | S0 count | Weight per selected task |
|---|---:|---:|---:|
| Direct, easy | 6 | 3 | 0.10 |
| Direct, hard | 5 | 3 | 1/12 |
| Direct, medium | 5 | 2 | 0.125 |
| Information retrieval, easy | 2 | 1 | 0.10 |
| Information retrieval, medium | 2 | 1 | 0.10 |

For the current manifest, freeze this concrete proposal:

| Task | Weight |
|---|---:|
| AudioRecorderRecordAudioWithFileName | 0.10 |
| ContactsAddContact | 0.10 |
| SimpleSmsSendClipboardContent | 0.10 |
| BrowserMultiply | 1/12 |
| MarkorCreateNoteAndSms | 1/12 |
| SaveCopyOfReceiptTaskEval | 1/12 |
| RetroCreatePlaylist | 0.125 |
| SimpleCalendarDeleteEventsOnRelativeDay | 0.125 |
| NotesMeetingAttendeeCount | 0.10 |
| TasksHighPriorityTasks | 0.10 |

This proposal was derived without candidate outcomes: enforce the quotas, maximize distinct app domains, then tag coverage, then minimize the sum of SHA-256 priorities from `42:<task_name>`, with lexical task IDs as the final tie-breaker.

Implement a deterministic preparation command, `python -m alpha_agents.tree_search.bridges.androidworld.prepare_subset --manifest androidworld_sets_manifest.json --seed 42 --output <run-manifest.json>`. It reads only task IDs, stage, family, difficulty, app-domain labels, and tags; historical outcomes and runtimes must not influence selection. Enumerate feasible quota-constrained subsets and apply the recorded ordering exactly, sorting strata/task IDs explicitly rather than depending on JSON row order. Serialize rational weights as numerator/denominator plus a derived numeric display value. The initial temporary implementation used the `bootstrapped.prepare_subset` path; the final bridge-owned command above follows the project ownership correction.

Check in a golden prepared manifest for the current source manifest. Store selected IDs, complement, stratum counts, weights, algorithm version, seed, source-manifest hash, and metadata coverage. Preflight verifies the golden output against a freshly prepared in-memory result; resume verifies hashes and never resamples. A changed source manifest requires preparing and reviewing a new run manifest.

Repository inspection reproduced the listed ten task IDs and five stratum counts. The selected subset covers 10 of the full set's 16 distinct `app_domain` labels and all 12 distinct metadata tags. Omitted labels are Broccoli, Files, OpenTracks, OsmAnd, Pro Expense, and Simple Draw Pro. These are metadata-label counts: composite labels such as `Gallery / Files` are not automatically equivalent to individual-app coverage.

Reports must show selected/full counts by stratum and app, tag/failure-mode coverage where supported, baseline runtime shares, and effective sample weight `1 / sum(w_j^2)` (approximately 9.80 weighted tasks for this subset; this is not a claim of statistically independent observations). Label unmeasured failure-mode coverage as unknown. App/tag coverage does not establish representative runtime, failure distribution, or generalization.

Display each task's weight and weighted contribution. A single changed success affects S0 utility by approximately 0.0833, 0.10, or 0.125 depending on its stratum; comparisons and reward explanations must expose these unequal increments.

Use the existing trusted runner’s normalized binary success predicate, explicitly recorded in the new objective version. Preserve raw AndroidWorld rewards separately. Do not reuse legacy aggregate scores as new S0 utility.

Existing benchmark discrepancy notes, including Audio Recorder’s filename issue, remain visible diagnostics. They do not authorize evaluator edits, task removal, or task-specific harness compensation.

### Screen validity and experimental success gate

Execution validity requires complete, correctly identified, trustworthy outcomes for all requested Screen instances. It is independent of how many tasks succeed. Genuine bounded agent failures are valid unsuccessful outcomes; infrastructure/evaluator failures remain missing or invalid evidence and follow the infrastructure retry policy.

Require an explicit `screen_gate` in the run manifest. Support `strict_all_success` and `execution_valid`. The current live pilot and baseline preparation freeze `execution_valid`: valid task failures remain diagnostic outcomes and permit S0 evaluation. Strict success gating remains an optional experimental configuration, not an agreed requirement. The original design specification did not establish that these five successes were mandatory. Strictness is an experimental capability-preservation hypothesis: it assumes these Screen tasks represent essential capabilities that must be retained, but their essential status has not been validated. Report this rationale and assumption rather than presenting it as benchmark validity.

Freeze one Screen instance per task at the declared seed, cache the first valid complete outcome, and perform no success-conditioned retries. Under the strict setting, a genuine stochastic failure rejects a child or stops root initialization. This risks false rejection and must be reported. Additional predeclared repeated-Seed Screen experiments belong to a separate gate/objective version; diagnostic repeats cannot replace an inconvenient first outcome. Changing the gate on resume is prohibited.

### Evaluation contracts and cache

Represent Screen, targeted checks, S0, and full Selection as distinct stage requests. Retain the protected Confirmation partition in the task manifest, but reject attempts to submit Confirmation through the v1 search/finalization workflow. Future Confirmation execution requires a separately enabled protocol and budget; it is not part of current acceptance.

A task instance key includes its template, instance index, generated parameters/seed, artifact hash, model cache identity, evaluator revision, fixture identity, runtime limits, and runner configuration. For now, the exact Qwen weight revision is optional and does not block execution. With no recorded revision, use the observed model name (`Qwen/Qwen3.8-27B-FP8`) and an explicitly unverified identity scoped to the run directory and endpoint. Keep that identity stable when resuming the same run and separate across different run directories; never describe it as a verified weight revision. This permits within-run evaluation reuse without relying on cross-run equivalence of unversioned remote weights.

Verify the configured AndroidWorld commit against the actual checkout and fingerprint the runtime package plus root Python/dependency entrypoints, including dirty and generated source files. Exclude measurement-output directories from this source identity. Include the observed source hash, actual trusted bridge, staged adapter, and cache implementation in evaluator provenance. Preflight must confirm imported AndroidWorld and Artemis paths belong to the configured checkout and assigned harness. Recheck source identity before preparation, cached-stage reuse, and each device execution; a change during execution is infrastructure evidence rather than benchmark reward. Retain matching before/after source attestations in completed task evidence and require them when recovering an interrupted publication.

Retain the runner’s task-name-based deterministic seed generation so scheduling or sharding cannot change task instances.

Complete stages require exactly one valid outcome for every requested instance. Reject duplicate, mismatched, missing, and invalid outcomes.

Cache compatible per-task evidence. Full Selection completes the ten-task complement when S0 instances and conditions match; aggregate all 20 tasks with full-set weights.

Keep the first completed S0 evaluation snapshot fixed during search. Additional repeats are separate evidence. V1 does not offer in-place score corrections; a correction requires an explicit new statistics version and deterministic backup replay.

### Shared device scheduler

Replace whole-candidate leases with task-scoped device leases for the new adapter.

A task job includes artifact and execution identity, stage, task instance, trial, timeout, estimated duration, reset requirements, and output location.

Each device executes one job at a time. Before every task, reconstruct or verify the frozen harness, reset device/app state, and run required preflight. Release the lease in a `finally` block; quarantine unhealthy devices.

Scheduling policy:

- Prefer search-critical Screen, targeted, and S0 work over background promotions.
- Round-robin between active trial evaluation batches.
- Within a batch, start longer predicted tasks first.
- While both trials have queued work, permit at most three running tasks per trial.
- Let one trial borrow idle devices when the other has no queued tasks.
- Keep promotion jobs opportunistic during search; prioritize them after workers drain.

Run targeted checks through the same queue when they require a device. The implementer can request checks through the coordinator, but receives no unaccounted device lease.

Supply an invocation-specific file channel during the native engineering Goal. The implementer may write one request file naming its attempt, a stable check ID, allowed task names, the mechanism, and a falsifiable expected observation; it may read the owned result directory and copied trusted trajectories. Protocol `targeted-check-file-v2` places that file at `check_input/check_request.json` inside a dedicated invocation-owned writable inbox, because Codex's sandbox helper cannot mount an exact writable file as a root. Keep surrounding request files and the result directory read-only; reject symbolic links in the inbox or request file. No command, seed, device assignment, or protected Confirmation request is accepted. Require the patch to remain stable while a request is pending; freeze an exact intermediate artifact and evaluate a separate workspace. Persist request identity, artifact, lifecycle, and actual evidence in SQLite. Identical check IDs and inputs are idempotent; changed requests need new IDs. Validate completed engineering handoffs against all admitted check results. The original Goal deadline remains fixed, and termination stops check admission while already admitted bounded device work drains. Partial Goal handoffs retain their engineering status without claiming that late evidence was inspected. These diagnostics use separate targeted cache identities and do not change canonical samples or judge reward context.

Use the trusted Artemis runner with explicit singleton task requests initially. Each task receives isolated output, database, trace, and temporary directories. Measure startup overhead before considering persistent runner optimization.

Retry an infrastructure-failed task once after reset within the declared job timeout/retry policy. Repeated infrastructure failure leaves the stage incomplete and the artifact ineligible, stops new admission, and preserves work for recovery. Classified bounded agent failures remain benchmark outcomes. Already admitted evaluation/cleanup jobs continue when new trial admission stops; promotions cannot starve their device access. No retry is rejected because of accumulated device-hours.

### Execution limits, Codex completion reserves, and cost accounting

There is no device-hour cap in v1. Remove device-hour admission checks, the 25% allocation rule, protected final-evaluation device-hour balances, minimum device-hour feasibility requirements, and device-budget-triggered stops. Do not implement these as required configuration or implicit defaults. Queue capacity and exclusive device leases remain scheduling constraints, not spending limits.

Keep the structural limits: 30 launched trials, three new attempts per trial, 90 attempts overall, three plans per harness, three attempted implementations per plan, depth eight, two trial workers, and up to six devices. Keep role/task deadlines and bounded infrastructure retries. These limit each operation and the amount of generated work while allowing admitted evaluations and final Selection to finish without a total device-hour cutoff.

A task attempt has a hard 2,100-second total device-occupancy envelope: at most 120 seconds for reset/startup/preflight, 1,800 seconds for task execution, and 180 seconds for process termination/cleanup/device release. Enforce phase and outer watchdog deadlines. The trusted supervised runner reserves one second inside the 1,800-second task envelope for publishing the cleanup transition, so the agent execution timeout is 1,799 seconds. On expiry, record cleanup before awaiting agent cancellation; cancellation, video/trace finalization and teardown consume the existing 180-second cleanup allowance. Preserve this reserve in execution receipts and the trusted-runner identity. A suppressed cancellation cannot become success; a stuck cancellation or unconfirmed cleanup remains infrastructure failure. This changes no historical outcome or frozen runner. A permitted infrastructure retry receives another complete envelope, including reset and cleanup. Quarantine a device if bounded cleanup cannot restore health; record the fault and do not keep allocating work to it. Keep the existing 300-second LLM invocation limit unless explicitly configured otherwise. Targeted checks declare their own timeout and retry limits before admission.

Register mandatory Screen/S0/targeted task jobs and their retry limits before an implementation is evaluated. Record actual device lease durations for initial attempts, resets, retries, and cleanup, including failed jobs. Cache hits are recorded as reused evidence, not new execution. Separate observed durations, expected-runtime forecasts, and timeout-based upper bounds; none is a device-hour spending reservation.

Retain the configurable cap of 2,000 top-level Codex role invocations, including retries. Removing the device-hour cap does not remove this distinct model-call limit. Maintain the transactional invariant `used + reserved_completion + free = invocation_cap`. Count orchestration invocations, not unbounded internal SDK goal turns/model calls; record internal usage separately and label that distinction in reports.

Before root evaluation, reserve its two reviewer invocations. At trial admission, reserve two value-assessment invocations (initial call plus permitted retry), even if the trial is expected to reach depth eight. Release them only after a persisted structurally-exhausted/empty-path decision makes them unnecessary or value/backup has settled. Before a new implementation, reserve its implementation invocation and both judge phases. Reserve proposal/policy invocations and their permitted retries before launching the owned operation. Completion invocations cannot be spent on new proposal, refresh, implementation, or promotion work.

If the remaining unreserved Codex capacity cannot fund another expansion/implementation, stop that continuation at its last valid frontier and use its protected value pair. If new trial admission stops because of the trial/attempt limit, model-call limit, operator stop, or outage, finish already admitted bounded evaluation/cleanup work and necessary value/backup operations. A model/evaluator outage can still leave recovery pending. Repeated assessment recovery requires new invocation capacity or an explicit invocation-cap increase; device evaluation is governed by its retry policy and has no cumulative hour-based stop.

### Final-Selection workload forecast

Configure up to three non-root nominees, default `k=3`, plus the root baseline. Under the frozen paired execution contract, every nominee/root already has ten S0 outcomes, so full Selection needs ten complement tasks per artifact. For reporting, compute a timeout-based upper bound allowing one infrastructure retry per task:

```text
upper_bound(k) = (k + 1) * 10 * 2 * 2100 / 3600 device-hours
```

| Non-root nominees | Artifacts including baseline | Missing Selection tasks | Timeout bound, no retry | Timeout bound including one retry/job |
|---|---:|---:|---:|---:|
| 1 | 2 | 20 | 11.67 device-hours | 23.33 device-hours |
| 2 | 3 | 30 | 17.50 device-hours | 35.00 device-hours |
| 3 | 4 | 40 | 23.33 device-hours | 46.67 device-hours |

These are reporting bounds, not expected runtime, required budget, or affordability tests. They exclude Confirmation entirely. Root/child Screen/S0, targeted checks, and optional promotions are additional workload. If a fresh full Selection run is necessary, use all 20 tasks in the forecast. Compatible cached outcomes reduce actual missing work after verification.

Do not reduce `k`, reject preflight, or stop evaluation based on these numbers. A smaller shortlist occurs only when fewer eligible distinct artifacts exist or the configured shortlist limit is smaller. Root timing supplies a separate expected-runtime forecast, with a 25% forecasting margin; this margin is not a resource-allocation percentage.

### Promotions, shortlist, and final Selection

During search, permit at most three distinct full-Selection promotions. A Screen-eligible artifact qualifies when its measured S0 utility strictly exceeds the provisional incumbent. Queue at most one promotion per artifact and enforce the three-promotion count atomically. Promotions use idle device capacity behind trial-critical work and may be deferred until search drains; no device-hour affordability condition applies.

Promotion evidence can inform subsequent development contexts but never replaces the frozen S0 U, canonical J, or combined F snapshots used for reward.

After trial workers finish and reservations are released:

1. Rank eligible non-root artifacts by measured S0 utility.
2. Select up to the configured `k`, using differing parent-plan lineage as the recorded diversity tie-breaker, then stable artifact ID. Fewer valid distinct artifacts produce a smaller shortlist without replacements from invalid nodes or duplicate artifact hashes.
3. Include the root baseline and register the entire missing Selection-complement batch with permitted retries. Evaluate the baseline and highest-S0 nominee first, then remaining nominees in shortlist order; compatible completed results are reused and identical artifact evaluations are deduplicated.
4. Complete full Selection for every nominee and baseline. Do not promote a partial full-set result to a winner score.
5. Choose the highest full-Selection utility; on ties prefer the root, then shallower depth, then stable ID.
6. Freeze and export that exact measured winner with the full-Selection report. Record `selection_status=completed`, `confirmation_status=not_run`, and `confirmed=false`.

Final Selection continues after the search trial/attempt limits or Codex invocation cap are reached because the trusted evaluator does not require a new Codex role invocation. Device-hour totals never block it. Infrastructure failure or explicit operator interruption must remain visible as incomplete coverage: report `final_evaluation_incomplete`, preserve the queued/completed jobs for recovery, and export any requested artifact as provisional rather than claiming the complete shortlist was compared.

No Confirmation tasks or trajectories are read, executed, or used to choose the winner. Reports state that S0 and full Selection are development evidence: full Selection can inform search through promotions and is used for final artifact selection. The winner's Selection score is not an independent estimate of generalization. Confirmation remains available as a later separately authorized evaluation with a frozen protocol.

### Operator interface and output

Extend the CLI with:

- `--preflight-only`;
- `--stop-after-trials N`, which drains the admitted trials;
- existing `--resume`;
- status, finalization, and artifact-export commands for the new run format.

Resume requires the same root, objective, gate, prompts, role configuration, evaluator contract, and structural search limits. An explicit operator-authorized Codex invocation-cap increase is the only budget exception and requires an audited grant/recalculation of protected model-call reservations. There is no device-hour cap to increase. Status is read-only; export writes only the requested frozen-artifact copy and never changes search statistics. Finalization can resume incomplete validation without restarting search.

Provide a JSON status snapshot and Markdown final report containing:

- launched/completed/failed/pending trials;
- attempts, valid children, and invalid-attempt rate;
- tree statistics and measured/predicted return contributions;
- device utilization, queue delay, durations, actual device-hours by stage, judge overlap/added latency, and Codex invocation usage/completion reservations;
- terminal-stop reason and capacity snapshot, selection-time prior versions, configured shortlist size, and observed versus expected/timeout-bounded costs;
- Screen-gate assumption, first-instance failures, subset coverage, task weights, per-task weighted AndroidWorld contributions, J dimensions/unknowns, lambda, F, and separate delta_U/delta_J reward contributions;
- S0 and full Selection results and coverage, plus `confirmation_status=not_run` and `confirmed=false`;
- selected artifact hash and reproducible runtime configuration;
- baseline comparison, per-task regressions, judge findings, and limitations.

Export the exact frozen winning harness and its configuration. Do not generate a final untested patch.

## 5. Implementation sequence, recovery, and acceptance tests

### Milestone 1: Contracts, persistence, and artifacts

Implement typed records, SQLite schema, writer ownership, transactions, model-call reservations, observational cost ledger, artifact freezing/reconstruction, and readable exports.

Acceptance:

- Frozen artifact survives reconstruction byte-for-byte with matching modes and symlinks.
- Required Artemis modules remain present.
- Concurrent reservations cannot exceed trial or implementation caps or consume another trial's protected Codex completion invocations.
- Two workers cannot publish expansion or refresh results for the same node/evidence version; stale fencing-token results are rejected.
- A second coordinator cannot own the run.
- Existing DGM imports and tests remain unchanged.

### Milestone 2: Deterministic controller with fake components

Implement plan registration, both PUCT levels, trial traversal, terminal handling, value requests, and transactional backup using fake proposer/evaluator/value services.

Required cases:

- With `lambda=0`, the original `0.60 → 0.65 → 0.62 → 0.70` AndroidWorld-only example with frontier value 0.04 and gamma=0.9 produces discounted returns `0.11696`, `0.0744`, and `0.116`.
- With `lambda=0.10`, parent `U=0.60,J=0.40` and child `U=0.70,J=0.80` yield `F=0.58` and `0.71`, reward `0.13`, and separate contributions `0.09` and `0.04`.
- Discount each measured/judge contribution and frontier prediction at its correct transition distance. Repeated unchanged U/J snapshots yield zero immediate reward, and the value target/bounds use F.
- Nonzero estimates at depth eight are backed up; genuine termination uses zero future return.
- Version prediction/backup and reject incompatible resumed statistics; report measured retained-artifact performance separately from allocation returns.
- All parent/child rewards reference the same frozen weight/rubric/objective; incompatible snapshots cannot be mixed.
- Both bookkeeping levels receive the same parent-relative return.
- Immediate reward is never counted twice.
- Stored traversal does not invoke implementation or add benchmark samples.
- Resampling creates siblings at the same depth.
- Depth eight uses zero value and creates no child.
- A depth-four exhausted node with no permitted outgoing transition uses the absorbing constant-utility convention; it is not labeled intrinsically unimprovable.
- Exhausted generation slots with eligible stored children remain traversable; pending work is not a terminal stop.
- A computational cutoff requests value against the frozen capacity snapshot despite an empty discretionary budget.
- A zero model prior receives a positive effective prior after flooring, and selections retain their exact prior version.
- Negative reward permits continuation.
- Invalid attempts consume caps but receive no fabricated utility.
- Empty failed trials make no Q update.
- Thirty launched trials never become 31.
- Reservation contention waits and reselects without deadlock.

### Milestone 3: Detailed prompts, native planning, Codex roles, and evidence

First author and review the complete sophisticated prompt set, including role-scoped Astra autonomy, concrete workflows, evidence/quality requirements, and developed output examples. Then implement the five Codex role adapters, native Plan-mode proposal handling, versioned plan catalogs/diversity audits, common role-request envelope, assessment schemas, frozen-plan engineering context, evidence snapshots, two-hour supervision, independent harness judging, and provenance.

Acceptance:

- IDs, priors, numeric bounds, and evidence references are validated.
- Every rendered role prompt contains its full objective, authority, workflow, autonomous completion, evidence, continuity, and developed output requirements; no generic brief/concise instruction remains.
- Native Plan-mode payloads are verified, and proposal handling accepts full completed plan items; implementer requests use the persisted native Goal API rather than ordinary-turn fallback; unsupported mode fails preflight rather than sending a literal `/plan` prompt in ordinary execution.
- Planning changes no harness files, while implementation autonomy remains limited to the frozen intervention.
- Proposal inputs include a versioned verification-scope contract: inspect actual existing tests, include every necessary test edit or new test path in the frozen scope, and explain the intended assertions. Read-only tests need no edit grant. A scope omission never authorizes silent scope expansion; adding a missing test path alone does not make a duplicate intervention diverse.
- Proposal inputs also include a versioned novelty-admission contract. Every emitted candidate declares `novelty_status` as `new_action` or `cross_parent_adaptation`; adaptations must identify an archived plan at a different exact parent artifact. Missing classifications and declared duplicates or scope-only repairs are rejected before admission. Discuss rejected ideas outside Candidate sections and permit a supported zero-plan result. These declarations supplement exact intervention uniqueness and manual semantic review; a valid label alone does not prove novelty.
- Value requests include a versioned continuation-role protocol alongside their frozen capacity/evidence snapshot: actual role model and effort, native Plan/Goal modes, invocation deadline, current planning admission and test-scope contracts, and whether an explicit implementation verification environment is configured. Describe its test command without granting the value role access to that environment. Configuration availability is distinct from evidence that a particular test ran or passed.
- The proposer reads the existing-plan catalog and closest full plans, records its archive review, rejects same-parent duplicates, and distinguishes novel mechanisms from paraphrases.
- Pairwise diversity examples, redundant candidates, justified cross-parent adaptations, incomplete archive access, and concurrent relevant plan publication exercise the novelty contract.
- Rich proposal/policy/value/judge outputs are manually reviewed on representative evidence before live search; prompt hashes and native mode persist with every request.
- Every role invocation and each permitted retry/phase receives a 7,200-second deadline; no five-/ten-minute assessment timeout remains.
- An invocation can run beyond the former short deadlines, while at 7,200 seconds it is cancelled with partial evidence retained. Use fake clocks/processes for these tests.
- Within-run SDK recovery preserves the original deadline; a separately metered retry has a new two-hour deadline.
- Only the implementation role can edit the disposable harness; assessment roles and the judge cannot mutate canonical artifacts, plans, measurements, or search statistics.
- Frozen-plan implementation prompts remove legacy easiest-first/task-selection instructions and require meaningful inspection/verification before completed status.
- A syntactically valid output with mismatched input IDs, missing required evidence, or unrun checks represented as successes is rejected or explicitly marked incomplete.
- Malformed responses follow the declared retry policy.
- Separate implementation attempts use separate contexts.
- Value outage retains the pending trial and stops admission.
- Exhausting discretionary Codex capacity leaves the active trials' protected value calls and permitted retries available.
- Exhausted recovery funding produces `recovery_budget_required`, never a zero bootstrap or unmetered retry.
- Root and child harness-code reviews overlap Android evaluation; execution review uses only canonical artifact evidence and no incoming-plan/parent/implementer narrative.
- The root receives a real artifact-quality review, and identical artifact/evidence/configuration identities reuse the canonical J regardless of incoming plan.
- Per-dimension insufficient evidence uses the declared neutral contribution and exposes unknown coverage; final-review outage instead leaves J/F unavailable.
- Code-phase timeout may remain partial; a final-phase timeout preserves Android outcomes but marks `awaiting_judge` and prevents combined-score backup. Recovery is explicitly metered and cannot add a second backup.
- At most two initial reviewer invocations per artifact, two hours each, with latency recorded.
- Confirmation evidence is excluded from every development context.
- Missing SDK/authentication/capabilities produce actionable preflight errors.

### Milestone 4: Staged Artemis integration and device queue

Implement singleton task execution, common queue, task-scoped leasing, stage aggregation, cache compatibility, strict Screen gating, and cost reconciliation.

Use mocked devices for concurrency tests and existing runner fixtures for outcome-contract tests.

Required cases:

- The deterministic preparation command reproduces the checked-in golden S0 task IDs, rational weights, source hash, strata, and coverage report.
- Reordering manifest rows does not change the prepared subset; altering historical outcomes/runtimes does not affect membership.
- S0 contains exactly the declared ten tasks with weights summing to one, and reports expose unequal per-task contributions.
- Task seeds remain identical across device assignments.
- Under the explicitly configured strict gate, a valid Screen failure prevents S0 and is not retried for success; a failed root stops initialization.
- Under the execution-valid gate, a valid Screen task failure remains diagnostic and permits S0; changing the gate on resume is rejected.
- Infrastructure failure stays distinct from agent failure.
- A retry does not duplicate the scored outcome.
- A task lease is released after success, failure, timeout, and cancellation.
- One trial can borrow idle devices; neither trial starves.
- Full Selection merges individual S0/complement outcomes correctly.
- Changing model, evaluator, fixture, seed, or artifact invalidates cache reuse.
- Device-hour accounting never denies admission or final evaluation, even when observed usage exceeds the forecast.
- Reset, retry, and cleanup retain their declared timeout envelopes and execute after new admission stops.
- Final-evaluation reporting reproduces the 23.33/35.00/46.67-device-hour bounds as informational forecasts only; configured shortlist size is not reduced for affordability.
- Confirmation task submission is rejected by the v1 workflow; no Confirmation costs or outcomes appear in search/finalization evidence.
- At most three distinct promotions are admitted; trial-critical jobs take priority, and verified cache reuse reduces actual execution without inventing new observations.

### Milestone 5: Recovery, finalization, and reporting

Persist checkpoints before and after every costly operation.

On restart:

- Reconcile outstanding work/model-call reservations against durable process/lease records before admitting work. If actual elapsed device occupancy cannot be established, record its timeout upper bound as an accounting bound; do not fabricate an observed duration or use that bound to deny further device work.
- Completed compatible task evidence is reused.
- Interrupted task jobs retain their consumed attempt count. Reset/retry uses the remaining declared retry allowance under the same task identity; exhausted retries require an explicitly requested bounded recovery round recorded separately, not an automatic fresh retry counter or a device-hour budget increase.
- A frozen implementation resumes evaluation without regeneration.
- An interrupted unfrozen implementation becomes an interrupted failed attempt; preserve its partial patch and logs.
- Pending value assessments are retried against the original frozen U/J/F/evidence/capacity snapshots only after renewed recovery capacity is reserved.
- A pending final harness judge review reuses already completed Android evidence and requires a newly reserved final-review invocation; no Android rerun or second backup occurs.
- Expansion and prior-refresh ownership is reclaimed only after process verification/termination, with fencing against late publication.
- A committed backup is never repeated.
- Reservations are released only after their owner’s recorded processes have exited or been reclaimed safely.

Inject crashes at attempt reservation, artifact freeze, task completion, stage completion, value persistence, immediately before backup, and immediately after backup. Each resumed run must preserve cost and cap accounting and produce exactly one backup per completed trial.

Finalization tests must verify Selection-based winner choice, baseline fallback, configured shortlist size, baseline/highest-S0 ordering and identity deduplication, shortlist freezing, Confirmation deferral/isolation, and explicit `final_evaluation_incomplete` status. A completed final report must state that the winner is unconfirmed. Model-call-cap tests must show that active value assessments retain their reserved invocations while new model work is denied, and device/final evaluation continues independently of accumulated device-hours.

### Live acceptance

Progress through:

1. Offline deterministic search using real temporary Git workspaces.
2. Artemis root preflight and baseline timing.
3. One-worker, two-trial live smoke run with real Codex assessments and one device.
4. Two-worker live smoke run with the shared device pool.
5. The configured 30-trial experiment with device-hours forecast and recorded for observation only; complete final full Selection after search drains.

V1 is complete when it can finish or correctly resume the search and final full-Selection lifecycle, export the exact selected artifact with Confirmation explicitly deferred, and explain every reward, prediction, visit, failure, and cost from persistent records.

Improved benchmark performance is an experimental outcome, not a software acceptance requirement.
