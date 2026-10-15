# Fixed-PUCT harness search

The implementation follows the existing project owners: core MCTS owns search
and authoritative state, the Codex integration owns role execution and prompt
assets, the AndroidWorld bridge owns staged/device evaluation, and shared
process utilities remain in infrastructure. Core imports neither adapter.

## Integration boundaries and motivation

The purpose of these boundaries is to replace a coding agent or evaluator
without changing tree selection, budgets, scoring, or backup. Import isolation
alone is insufficient: core must also avoid reading adapter runtime objects,
cache internals, and device schedulers.

[`ports.py`](ports.py) defines `RoleGateway`, `StagedEvaluator`, `RoleSettings`,
and `StageEvidence`. The agent port declares request preparation/serialization,
context enrichment, execution, preflight, usage, and diagnostic support. The
evaluator port declares owner sessions, stage preparation/execution, evidence
verification, preflight, diagnostic support, and interrupted-work reconciliation.
Core calls these operations directly rather than inspecting optional methods.

`RoleAdapter` and `EvaluationAdapter` provide explicit defaults for local,
uncached implementations. A role adapter must implement `prepare_request` and
`invoke`; its request must satisfy `RoleRequest`, including `to_record()`.
The request need not be a dataclass. An evaluation adapter must implement
`evaluate`. Implementations with persistent evidence or external processes must
override `verify_stage` and `reconcile_interrupted`; their no-op defaults are
only appropriate for integrations with nothing external to verify or reclaim.
Shared evaluators must be thread-safe or return an owner-specific session from
`for_owner`. These sessions borrow external resources; they do not close them.
Both ports must declare `supports_checks=True` before core opens an implementer
diagnostic channel. An unsupported check invocation fails explicitly.

Agent-specific execution modes, permissions, verification tools, and prompt
assets remain in the agent adapter. `enrich_context` describes these details
to the implementer and value role without exposing the runtime object to core.
The AndroidWorld evaluator owns its task-cache reconciliation and runner paths.

`composition.build()` returns a `SearchApplication` that owns the coordinator
and device scheduler. Use it as a context manager, or call `close()` in a
`finally` block. It drains the scheduler before closing search state and closes
each resource once. Partial construction also cleans up acquired resources.
`FixedPUCTCoordinator` itself owns only its search store; it has no scheduler
attribute. Alternative applications may construct a coordinator directly with
their own ports and explicit, stable `runtime_identity` metadata.

```python
from alpha_agents.tree_search.composition import build

with build(resolved_config, output_directory) as application:
    application.preflight()
    result = application.run()
```

The existing Screen/S0/Selection protocol, binary task outcomes, scoring,
shortlisting, and trial limits are retained. This refactor does not introduce
arbitrary evaluation stages or objectives. The production CLI still assembles
Codex and AndroidWorld/Artemis; another integration supplies different ports
and assembly code rather than editing the search core.

### Frozen identity compatibility

`FixedPUCTConfig.from_mapping()` accepts `role_invocation_cap` and
`role_timeout_seconds`. Their historical `codex_*` spellings remain accepted;
conflicting values are rejected. Serialization retains the old field names so
equivalent configurations keep their fingerprints. Status adds
`role_invocations` and retains `codex_invocations` as a compatibility alias.
Production adapters retain the `codex`/`android` preflight labels and `codex`
cost label; alternative adapters use their declared `telemetry_name`.

Codex context enrichment preserves the previous native Plan/Goal context,
rendered requests, and prompt hashes. Moving source files or changing trusted
evaluator code still changes the source hash used by production run identity.
An old frozen run may therefore reject resume with this evaluator version.
Keep its matching software or create a fresh run; do not rewrite stored hashes
or historical outcomes to bypass that check. Status inspection remains available.

Replacement search, resume, export, declared capabilities, and lifecycle tests
are in [`test_mcts_ports.py`](../../tests/test_mcts_ports.py). That replacement
test executes in a fresh process and verifies that no Codex or AndroidWorld
integration modules were loaded.

Inspect and edit the [Codex prompt assets](../../mutators/coding_agents/codex/roles/prompts/README.md).
Detailed role text and native Goal objectives are Markdown; static request
guidance is JSON. Version v22 pins all these instruction assets in the library
identity. Native workers verify the admitted library and rendered-prompt binding
before starting a model turn. Changing assets requires compatible run identity;
old unbound native request files are historical evidence and cannot be silently
replayed under a different prompt version.

All six role templates include explicit evidence and completion rubrics. Judges
share `artifact-quality-rubric-v2`, with five behavioral anchors for each of three
quality dimensions. Confidence does not rescale scores; unsupported dimensions
remain explicitly unknown. Code review is preliminary and independent execution
review owns the final assessment. Other roles use their existing plan, prior,
engineering-status and discounted-value outputs rather than extra ordinal scores.
Static instructions and JSON guidance are domain-neutral; adapter context supplies
application semantics, source/evidence formats and optional diagnostics. No output
schema, utility weight or core interface changes are needed. The rubric/prompt
identities prevent incompatible resumes. `test_role_rubrics.py` covers all-role
rendering, domain neutrality, anchor cardinality and identity binding; live grader
agreement and calibration require separate experimental evidence.

Each successful harness expansion registers exactly three distinct frozen plans.
Each plan has three implementation slots, filled through MCTS selection rather
than a batch of nine candidates. A response with fewer than three admissible
plans leaves expansion incomplete and open for recovery. It cannot establish
terminal exhaustion. Trial budgets and interrupted work can leave implementation
slots unattempted; report those slots explicitly. Earlier runs that admitted
fewer plans retain their original frozen protocol and evidence.

The shared CLI can inspect older stored runs without applying current runtime
settings. Resume/finalize execution still verifies frozen configuration, prompt,
model/cache scope, evaluator source, and fixture identities. If those change,
retain the earlier evidence and use matching software or a fresh run. A package
move is not a reason to relabel task outcomes or recompute old rewards.


This controller selects frozen intervention plans with PUCT, implements each
attempt through native Codex `/goal`, evaluates exact harness artifacts, and
backs up measured combined-score changes plus a frontier value prediction.
`max_depth` caps actual tree expansion. Value predicts expected discounted
continuing net improvement under the declared policy, with no finite horizon or
remaining-budget input. `discount_factor` defaults to the experimental starting
point 0.9 and must satisfy `0 < gamma < 1`. Rewards and value use the same
frozen F = (1-judge_weight) U + judge_weight J. Backup applies
`G_t = r_t + gamma * G_(t+1)`, separately discounting measured/judge/predicted
contributions and updating both bookkeeping levels exactly once.

A nonempty path ending at maximum depth still invokes value and can back up a
nonzero forecast. Hypothetical continuation ignores actual depth/global budget
cutoffs, preserves used local branching capacity, fills unused implementation
slots before traversing stored eligible children, and updates evidence, priors,
and discounted statistics under the same procedure. Pending work/outages are
not termination. Empty paths and proven structural exhaustion retain zero
future return without an invocation. Snapshots and assessments bind
`discount_factor` and `continuation_policy_id` to artifact/objective/evidence
identities. Prediction is `fixed-puct-discounted-continuing-v4`; backup is
`discounted-component-backup-v2`; prompts are v22. Old undiscounted N/W/Q cannot
resume into this contract. Status/export inspection remains available; start a
new run rather than silently relabeling older statistics. `value_horizon` is
removed. Status and final reports show best measured retained-artifact quality
separately from discounted search allocation returns.
Proposer and policy roles receive the same discount/policy contract and compare
expected discounted gains, prerequisites, regressions and timing. Judges retain
artifact-only scoring evidence. Discount steps count harness transitions,
including stored traversal, not failed attempts, plan bookkeeping, tokens or
elapsed time. Temporary gains followed by reversals can have positive discounted
return; report discovered artifact performance separately.

Status/final reports include `value_calibration`. Snapshot traversal cutoffs
exclude earlier observations. Later completed trial suffixes with matching
policy/discount provide discounted measured prefixes and bounded unknown tails.
For K transitions, the tail lies in `gamma^K * [-F(H_K), 1-F(H_K)]`; only genuine
exhaustion makes it zero. No later model value is treated as observed return.
These descriptive observations are adaptively selected and can reuse correlated
evidence; they do not establish calibrated expectations. Old snapshots without
cutoffs are unavailable. Exploration coefficients retain their current defaults;
calibrate them against discounted Q using matched budgets and measured retained
performance. Diagnostics do not automatically tune them or add a cost objective.

On node reselection, new completed/invalid implementation outcomes or retained
Selection evidence trigger a versioned policy refresh before edge reservation.
Refresh ownership is fenced; peer workers wait. Archive-sensitive expansions
share one persisted publication reservation across nodes, acquired atomically
with node ownership. Other workers can implement, evaluate, traverse stored
edges, and refresh priors while a proposal is assessed. A worker needing another
expansion waits without spending model calls. The same heartbeat and verified
stale-owner reclamation release this reservation after interruption. If the
publication check nevertheless finds a stale catalog, the expansion uses its
remaining reserved proposal/policy retry to review the updated archive and
reassess the candidates.
The recheck is a fresh native Plan invocation with the full previous proposal,
updated index and explicit diversity task, each with its own two-hour allowance.
It does not reset the call budget or borrow protected completion reservations.
Repeated catalog changes beyond that allowance leave expansion open for recovery. Calls and the permitted retry
are reserved separately from trial completion. Refreshes preserve frozen plans,
all N/W/Q statistics, and the exact prior versions recorded by older selections.
Visit counts and pending reservations alone do not trigger model calls. A failed
refresh retains the last valid distribution and stops admission for recovery.
Proposal uses native `/plan`. Each role receives its complete detailed prompt
and one 7,200-second invocation deadline. Named permission profiles restrict
role reads to the assigned workspace, explicitly supplied evidence, own prompt
files, and system tools. Judge profiles do not grant reads to plan archives or
earlier model-authored evidence paths. Loaded legacy `sandbox_mode` settings
fail preflight because they override profile selection; remove those settings
before starting this controller. Preflight also initializes an ephemeral thread
with the restricted profile, without starting a model turn or Goal. This detects
host sandbox initialization failures before admission. Each invocation persists its permission profile.
Live sandbox enforcement remains part of live acceptance, separate from adapter
tests.

Before an implementer starts, the adapter records which root `.agents`, `.aws`,
and `.codex` directories are absent in a trusted runtime registry outside its
write permissions. Codex may create these empty guard directories for sandbox
mounts. Artifact snapshots omit only registered, initially absent roots while
they remain empty real directories and are absent from the exact parent.
Existing source directories, any content, and symbolic links are never hidden
by this rule. The same artifact path handles intermediate checks and recovery.
Engineering handoffs use explicit per-check execution/status fields and exact
evidence arrays; a complete native Goal can still yield partial engineering
when required checks or dependencies remain unavailable.

[`examples/fixed_puct.json`](../../examples/fixed_puct.json) records the current experiment choice: `gpt-6-luna`
with `low` effort for every Codex role, and `Qwen/Qwen3.8-27B-FP8` at
`http://localhost:8001/v1` for the GUI agent. Replace the remaining placeholder
interpreter/binary paths and AndroidWorld/fixture identities before running it.
The Qwen weight revision is optional for the current experiment. Leave
`evaluation.model_revision` null to use the model name and an explicitly
unverified cache identity scoped to the run directory and endpoint. Resuming
that directory keeps the identity stable; separate runs do not share it.
Explicit Codex provider settings use the owned `alpha_agents_` namespace,
which current Codex requires when configuring a built-in provider's endpoint.
The Codex provider, endpoint and authentication mode are explicit; there is no
model or reasoning fallback. The Android interpreter must contain the Artemis
and AndroidWorld dependencies. Paths resolve relative to the configuration file.
An optional `codex.runtime.verification_python` points to a prepared virtual
environment for non-device implementer tests. Its exact environment directory
is granted read access only to implementation roles; parent directories are
not exposed. The supplied pytest command uses that environment without
expanding the frozen plan's edit scope. Interpreter paths preserve virtual
environment symlinks. Omit this setting to preserve an existing run's runtime
identity; changing it requires a new run configuration.
For an existing ChatGPT Codex login, configure `auth_mode="codex_login"`
and `base_url="https://chatgpt.com/backend-api/codex"`. For an API key,
use the example's `environment` authentication and `https://api.openai.com/v1`.
These credentials and endpoints are distinct: sending a ChatGPT login token
to the API-key endpoint can fail with HTTP 401 and `api.responses.write`.
See [official login routing guidance](https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway).
The adapter preserves the explicit endpoint rather than silently rerouting it.

Proposer contexts include versioned test-scope and novelty-admission contracts.
Plans must permit their necessary test edits and explicitly classify every
candidate as a new action or a cross-parent adaptation. Declared duplicates
and scope-only repairs are inadmissible; adaptations require a reviewed plan
at a different parent artifact. Exact intervention uniqueness and semantic
review still matter: a status label alone does not prove diverse behavior.

`androidworld_revision` is the full expected checkout commit SHA. Production
composition verifies that HEAD and records an observed runtime source hash,
including dirty Python entrypoints and generated/untracked package files.
Local edits are supported through that content identity; a clean checkout is
not required. Root measurement-output directories are excluded. The task cache
also identifies the actual staged adapter and trusted bridge code. Preflight
checks that dependency imports resolve to the configured AndroidWorld checkout
and assigned Artemis harness. Source drift blocks preparation, cached-stage
reuse, or task execution; drift during execution is an infrastructure failure,
never a benchmark outcome. Successful task evidence retains matching before/
after source attestations, which are required for interrupted-result recovery.

Create each new experiment under `runs/experiments/<experiment-name>/` from the
repository root. In the commands below, `RUN` means that experiment directory;
keep its configuration, logs, state, artifacts, evidence, and plots together.
Resume using the original directory so persisted identities and evidence paths
remain valid.

Run preflight, then a bounded trial smoke test:

```sh
python -m alpha_agents.tree_search --config CONFIG.json --output RUN --preflight-only
python -m alpha_agents.tree_search --config CONFIG.json --output RUN --resume --stop-after-trials 2 --no-finalize
```

The smoke stop drains admitted trials and leaves the remaining trial budget
available. Resume the same frozen configuration to finish search and final
Selection:

```sh
python -m alpha_agents.tree_search --config CONFIG.json --output RUN --resume
python -m alpha_agents.tree_search --command status --output RUN
python -m alpha_agents.tree_search --command finalize --config CONFIG.json --output RUN
python -m alpha_agents.tree_search --command export --output RUN --artifact SHA256 --destination EXPORT
```

During an engineering Goal, the controller supplies an invocation-specific
`device_check_channel`. The implementer writes a scoped JSON request naming
allowed tasks, the mechanism, and a falsifiable expected observation, then
waits without editing its patch. Protocol `targeted-check-file-v2` uses an
invocation-owned writable `check_input` inbox containing `check_request.json`;
surrounding request files and results remain read-only. Symbolic-link inboxes
and request files are rejected. The controller freezes that intermediate
artifact and services targeted jobs through the shared device queue, normal
cache/retry/cleanup protocol, and observed-cost ledger. Read-only result files
and copied trusted trajectories return to the same Goal for diagnosis and
repair. Stable check IDs are idempotent; changed inputs need a new ID.
Protected Confirmation and arbitrary commands/seeds/device assignments are
rejected. The check channel does not reset the original two-hour Goal deadline.
After Goal termination, new check admission stops and admitted device work
drains. Completed engineering handoffs must match every admitted diagnostic's
actual artifact and outcomes; partial Goal status remains partial even when a
late diagnostic finishes. Targeted samples never replace canonical outcomes
or enter the artifact judge's reward context. Status and final reports retain
the independent check-request ledger.

`--no-finalize` separates search completion from final Selection. SIGINT/SIGTERM
requests stop new admission while admitted bounded work drains. Pending
assessments remain visible and require `--resume`; they never become zero
predictions or neutral judge rewards. An explicitly requested invocation-cap
increase is audited:

```sh
python -m alpha_agents.tree_search --command grant-invocations --output RUN \
  --invocation-cap 2200 --recovery-reason "Additional bounded assessment recovery"
```

SQLite (`tree.sqlite3`) is authoritative. Artifacts and execution workspaces are
separate. Validated absolute code citations from review workspaces are stored
as relative `code:` references bound to the request artifact, retaining line and
column locations. Raw responses and citation mappings remain available for
audit. This permits response recovery without opening sibling workspaces.
Each actual role invocation records observed elapsed time and available SDK/CLI
usage with its reported scope. Missing usage stays unknown. Cached role-response
reuse has a separate ledger and consumes no new invocation or observed role cost.
For native Goal runs, telemetry also retains the SDK's durable `tokensUsed`
counter, lifecycle status, and available usage-time/update counters when the
saved Goal thread and objective match the invocation. This checkpoint remains
separate from adapter turn usage, including after an interrupted run with no
final native result. Neither source is labeled verified billing, and an older
checkpoint does not claim to measure work after its recorded update.
Each newly published prior version stores its full validated policy comparison,
uncertainty, evidence, and original invocation/model/prompt provenance alongside
raw, normalized, and effective priors in the same SQLite transaction. A content
hash binds the assessment; cached responses retain their original invocation.
Frozen continuation bundles include the assessment for their exact prior version.
Older numeric-only records remain explicitly without policy reasoning.
Frozen plans are exported as read-only `plan_archive/<identity>/PLAN.md`
files with SHA-256 hashes in SQLite metadata and proposal catalogs. The complete
catalog supplies every intervention, status, attempt/outcome summary, and exact
file reference. Full novelty/diversity/provenance records remain in read-only
`PLAN.json` files beside each plan. The proposer reads relevant full documents
through those paths; plan bodies are not duplicated across the entire initial
archive context, and no catalog entries are omitted. Missing
exports are rebuilt from SQLite; changed exports are rejected. Each archived
attempt additionally points to a hash-verified, read-only
`evidence_archive/<identity>/ATTEMPT.json` containing the exact parent/child,
available Screen/S0/Selection/targeted outcomes, full harness-judge findings,
score provenance, and explicit unavailable evidence. Trusted evaluation
references grant access to the corresponding task evidence, while arbitrary
paths in judge narratives grant no additional reads. SQLite retains each
immutable evidence snapshot; later outcomes create a new bundle and preserve
older role inputs. Missing older bundles are rebuilt before role execution.
Judge roles cannot read these archive files. Task cache identities include model/evaluator/fixture versions, seed,
limits and runner behavior. Valid failures remain cached. Targeted diagnostics
cannot replace canonical samples. A task gets one infrastructure retry; unknown
interrupted device occupancy is reported as a timeout bound, not an observed cost.
Queue admissions, assignments, elapsed callback work, and unstarted/interrupted
dispatches persist separately from cached outcomes. Status and final reports
show queue delay and assigned-slot utilization per coordinator session, using
configured device count and that session's first-to-last dispatch window.
Android device lease hours remain separate. Interrupted callback durations stay
unknown, and restart downtime is not merged into one utilization window.
`runtime_forecasts.json`, status, and the final report publish informational
Selection workload scenarios before search and update them after baseline
measurement. The paired S0 complement bounds include one infrastructure retry
per task; a separate bound covers fresh full Selection. Expected device hours
use complete observed root S0 lease costs, including retries and cleanup, and
apply a separate 25% forecasting margin. Frozen stratum means predict their
complement workloads and expose measured baseline runtime shares. Without
stratum metadata the estimate explicitly uses the pooled root mean. Missing
or interrupted lease timings leave estimates unknown. Two-hour code and
execution reviewer bounds are shown separately from device hours. Forecasts
never stop admission, change the shortlist, or serve as a resource allocation.
SQLite also records each assessment's monotonic phase boundaries, including
code-review/Android overlap, code wait after canonical S0, execution-review
duration, and added continuation latency until a usable score is ready. Status
and the final report expose these observations separately from actual Codex
invocation costs. Compatible cached-score reuse does not create another review
sample. After verified process reconciliation, unfinished records become
interrupted without invented endpoints; known completed intervals remain
available and missing durations stay unknown. Root forecasts link the baseline
review's observed intervals beside the separate two-hour timeout bounds.
Futures complete after slot release and quarantine bookkeeping; queued-future
callbacks execute outside the scheduler lock. There is no device-hour cap. After the original two infrastructure attempts are
exhausted and the task has no active execution, an operator can authorize one
bounded recovery round (two more attempts) for that exact job:

```sh
python -m alpha_agents.tree_search --command grant-task-recovery --output RUN \
  --task-job JOB_ID --recovery-id UNIQUE_ROUND_ID \
  --recovery-reason "Fixture repaired after exhausted infrastructure retries"
```

Then resume the run normally. Repeating the same recovery ID and reason is
idempotent; further rounds need new explicit grants after prior attempts are
exhausted. SQLite retains all original ordinals, contracts, costs and reasons.
Completed successes and valid benchmark failures cannot receive recovery grants.

`score_breakdowns.json` and `FINAL_REPORT.md` expose every node's task weights,
weighted canonical contributions, U/J/F, judge dimensions and unknowns, and raw
and weighted parent rewards. Complete available sources must reconcile with
frozen scores; unavailable evidence stays explicit. Predicted contributions
remain in separate backup records, whose capacity is the original value-context
snapshot; capacity at backup time is recorded separately.

Value assessments receive immutable `CONTINUATION.json` bundles frozen in the
same SQLite transaction as controller capacity. These retain every reachable
node's canonical evidence, development evaluations, full registered plans,
exact prior distribution, and previous value findings with their original
capacity identities and summaries. The initial prompt supplies the compact
capacity index and a hash-verified full bundle rather than duplicating all
evidence inline. Exact harness copies under `value_code_views/<artifact>` are
read-only and verified before and after assessment. Only the value role gains
reads to the declared code views and continuation file. Recovery preserves the
original evidence cutoff and rebuilds missing files from SQLite; later outcomes
cannot change an already frozen prediction context. Previous capacities omit
nested evidence to avoid recursively duplicating the entire assessment history.

Final Selection compares the frozen shortlist and baseline using complete
AndroidWorld outcomes. Search judge scores do not determine this winner.
`winner/` contains the exact selected harness; sibling export metadata and
`FINAL_REPORT.md` retain configuration and evidence. Confirmation is deferred,
and reports explicitly label the winner **unconfirmed**.

Offline tests exercise deterministic trial workers, subprocess watchdogs,
concurrent task queues, compatible-cache reuse, crash recovery, and finalization.
The full software lifecycle fixture runs all 30 trials with two workers and three
registered plan branches per expanded node, checks exactly-once backups and
local/global limits, finishes Selection, verifies the exact winner export, and
resumes the completed run without additional model calls. Initial and resumed
run responses use the same structure and report finalization status after it
finishes.
Live Codex and AndroidWorld acceptance is a separate requirement; an offline
green suite does not establish benchmark improvement or live operational readiness.

Prepare the deterministic S0 manifest through the bridge-owned command:

```sh
python -m alpha_agents.tree_search.bridges.androidworld.prepare_subset --manifest androidworld_sets_manifest.json --seed 42 --output RUN_SUBSET.json
```

The checked-in reference is `bridges/androidworld/s0_golden.json`. Selection
weights and IDs remain unchanged by relocating the preparation script.
