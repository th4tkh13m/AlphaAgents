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

### Unified engineering review

Root and child assessment collect canonical Screen/S0 evidence before one
`judge_review` invocation. The independent reviewer inspects code and execution
under `artifact-quality-rubric-v3`, with ten criteria in each of four dimensions.
`judge_review.py` owns the closed report schema, cross-field validation, and
scoring contract. Each rated criterion is an integer from zero through four.

Raw J equals `sum(rated scores) / (4 * rated count)`, in [0, 1]. Unknown and
non-applicable criteria remain explicit and are excluded from that denominator;
no rated criteria makes the review unavailable. A confirmed manipulation finding
forces effective J to zero while retaining raw J and the integrity conclusion.
Reports expose individual criteria, counts, normalized dimension summaries, and
contributions to effective J. Combined utility still uses its frozen judge weight.

Reservations fund one review per assessment. Recovery reuses canonical evaluation
and compatible persisted reviews, and reissues only unavailable review work.
A fresh rubric/prompt identity prevents reinterpreting historical experiment
ratings under this contract; historical score reporting retains legacy semantics.

### Frozen identity compatibility

`FixedPUCTConfig.from_mapping()` accepts `role_invocation_cap` and
`role_timeout_seconds`. Their historical `codex_*` spellings remain accepted;
conflicting values are rejected. Serialization retains the old field names so
equivalent configurations keep their fingerprints. Status adds
`role_invocations` and retains `codex_invocations` as a compatibility alias.
Production adapters retain the `codex`/`android` preflight labels and `codex`
cost label; alternative adapters use their declared `telemetry_name`.

Proposer and implementer use persisted native Goals in default collaboration
mode. The proposer remains read-only and returns its full Markdown design and
fenced JSON summary as the completed Goal's final response. Validation repair
updates the goal context file and resumes the same objective and deadline.
The implementer returns its implementation JSON. No native Plan preset is
required by runtime preflight. Rendered requests and prompt-library hashes bind
these role-specific objectives and output contracts. Moving source files or changing trusted
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
guidance is JSON. Version v34 pins all these instruction assets in the library
identity. Native workers verify the admitted library and rendered-prompt binding
before starting a model turn. Changing assets requires compatible run identity;
old unbound native request files are historical evidence and cannot be silently
replayed under a different prompt version.

All active role templates include explicit evidence and completion requirements.
The unified judge uses `artifact-quality-rubric-v3` and its forty-criterion schema;
confidence does not rescale scores. Proposer, policy, implementer, and value keep
their design, prior, engineering-status, and discounted-value outputs. Static
instructions are domain-neutral, with adapter context supplying application
semantics, source/evidence formats, and optional diagnostics. Production judge
protocol identity includes the normalization, missing-evidence, and integrity
rules as well as prompt identity. Legacy templates and snapshot aggregation remain
available for explicit historical inspection, without relabeling their ratings.
Software tests establish these contracts; empirical reviewer calibration remains
separate experimental work.

Each successful expansion makes three sequential proposer invocations. Each
returns one technical design document and a fenced JSON summary. Calls two and
three receive summaries of earlier plans at that node and must describe a
non-overlapping mechanism. Exact normalized duplicate interventions are rejected;
semantic overlap remains an explicit proposer responsibility. All three documents
are published atomically after policy assessment. A failed slot leaves expansion
open; recovery can reuse earlier validated responses without another model call.
Expansion reserves two calls per proposal slot plus two policy calls, with unused
reservations released at publication or failure. Each frozen plan retains three
implementation slots filled through MCTS selection.

Validated implementer JSON is stored immutably in `implementation_reports`, keyed
by attempt. Proposer context contains controller-curated `prior_implementations`
from the selected lineage in chronological order, including the current parent
implementation and excluding the root and sibling implementations. Curated fields
include changes, verification, diagnostic outcomes, blockers, and limitations.
Canonical scores are identified separately from implementer claims. Older attempts
without stored JSON are explicitly unavailable; history is never fabricated.
Inline summaries do not grant access to paths mentioned in their contents.

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
not termination. Empty paths, proven structural exhaustion, and qualifying
single-task success retain zero future return without an invocation. Snapshots and assessments bind
`discount_factor` and `continuation_policy_id` to artifact/objective/evidence
identities. Prediction is `fixed-puct-discounted-continuing-v6`; backup is
`discounted-component-backup-v2`; prompts are v34. Old undiscounted N/W/Q cannot
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
Refresh ownership is fenced; peer workers wait. Expansions retain one persisted
publication reservation across nodes, acquired atomically with node ownership.
Other workers can implement, evaluate, traverse stored edges, and refresh priors
while proposals are assessed. The existing heartbeat and verified stale-owner
reclamation release this reservation after interruption. New-format proposers
receive only ancestry implementation history and earlier plans at the same node;
they do not review or recheck the global catalog. Calls and permitted retries are
reserved separately from trial completion. Refreshes preserve frozen plans,
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
granting access to protected benchmark or evaluator files. Interpreter paths preserve virtual
environment symlinks. Omit this setting to preserve an existing run's runtime
identity; changing it requires a new run configuration.
Prepare the harness's declared verification dependencies in that environment,
including required development tools such as Ruff; a working pytest runner
does not imply that a linter is installed. Use disposable workspace copies for
post-run checks and disable tool caches where appropriate (`ruff check
--no-cache ...`). Never run cache-writing tools against frozen artifact content.
For an existing ChatGPT Codex login, configure `auth_mode="codex_login"`
and `base_url="https://chatgpt.com/backend-api/codex"`. For an API key,
use the example's `environment` authentication and `https://api.openai.com/v1`.
These credentials and endpoints are distinct: sending a ChatGPT login token
to the API-key endpoint can fail with HTTP 401 and `api.responses.write`.
See [official login routing guidance](https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway).
The adapter preserves the explicit endpoint rather than silently rerouting it.

Proposer contexts include versioned candidate-edit and novelty-admission contracts.
New intervention metadata contains `target`, `operation`, `conditions`, and
`mechanism`. Detailed plan prose provides a senior-to-junior implementation
handoff: current behavior, intended flow, ordered source edits and integration,
worked examples, and tests with acceptance criteria. Settings and behavioral
rules belong in those instructions rather than a separate `parameters` field.
Legacy parameters remain readable and retain their original identity.
Plans do not declare a file edit allowlist. Implementors may change candidate
harness files needed for the intervention, including supporting code and tests.
Legacy plan `scope` metadata is retained for identity compatibility and is not
used to reject diagnostic, final, or recovered artifacts. Benchmark/evaluator
and evidence protections remain in force.
Plans must identify meaningful verification and explicitly classify every
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
file reference. Controller summaries and provenance remain in read-only
`PLAN.json` files beside each plan for archival inspection; legacy records also
retain their original novelty and diversity fields. New proposer inputs
use JSON-curated lineage summaries and node-local existing plan summaries rather
than the complete archive catalog. Missing
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

### Structured value forecast validation

The active value role returns a closed `value-v2` report. Core `value_forecast.py`
validates its baseline, exact snapshot/policy identities, six rubric assessments,
scenario probabilities and transitions, discounted remaining continuation, final
aggregation, sensitivity, and signed bounds with a declared 1e-9 absolute
arithmetic tolerance. Subjective probabilities and causal claims remain model
judgments; arithmetic validation is not empirical calibration. Nonterminal
completed reports require sensitivity analysis.

Ancestry implementation summaries are curated from authoritative JSON and frozen
into the frontier snapshot before forecasting. Recovery reuses those summaries
and the original continuation evidence. Their embedded prose does not grant
filesystem reads or arbitrary citation authority. Full forecasts are retained in
value provenance and exposed with calibration/status records. Invalid or
incomplete reports preserve the pending trial without publishing a convenient
zero or backing up statistics. Existing explicitly recorded manual/historical
scalar provenance remains readable; new model responses require value-v2.


### Storage lifecycle (v29)

Read-only role invocations and value continuation views share one verified code
copy per artifact under `value_code_views/<artifact_id>`. Implementers and device
executions still receive independent writable materializations; writable files
are never hardlinked to frozen source. Recreating a missing shared view preserves
its artifact path and validated-response reuse, without a new model invocation.
A corrupt existing view fails verification rather than being silently repaired.

Before a completed task's evidence inventory is sealed, the trusted publisher
creates `.alpha_inspection_sqlite/index.json` and one standalone inspection DB per
original SQLite file. Stable DB/WAL or rollback-journal copies feed SQLite backup; the destination
uses DELETE journal mode and is closed before publication. Source DB/WAL/SHM
hashes and snapshot hashes are bound to the sealed inventory. Role inputs expose
`sqlite_inspection` with exact read-only immutable URIs; roles inspect these
snapshots directly instead of making their own database copies. An optional
SQLite preparation failure is recorded as an unavailable inspection with its
source identity and error; it does not replace a valid canonical task outcome. Historical
inputs without these declarations retain the safe scratch-copy fallback.

Terminal validated roles may evict derived SQLite scratch files after their
owned processes have exited, retaining scripts, logs and retention receipts.
Completed implementations retire unneeded source only after their report and
frozen-attempt checkpoint are durable; cited local files and `.pytest_cache`
verification records stay at their original paths. Partial or interrupted
engineering workspaces and inputs with still-live owned subprocesses remain intact. Completed evaluation inputs are retired
after canonical evidence publication; pending or failed inputs are preserved.
Frozen artifacts, canonical evidence, role responses, scores and backups remain.

Diagnostic handoffs expose independently inventory-checked read-only directory
projections. Their regular files share canonical evidence inodes, rather than
copying the bytes again. Role permissions keep both locations read-only; this
mechanism is never used for mutable harness files. Unsupported hardlinks fall
back to copies and report their actual storage mode. Deleting a projection
leaves canonical evidence intact.

These changes affect trusted runner and prompt identities. Use a fresh run for
the new protocol; existing experiment evidence and frozen statistics are not
rewritten or garbage-collected by upgrading the runtime.


### Single-task success stopping

For a fresh single-task search, `stop_on_task_success` defaults to `true`.
Single-task evaluation defaults to `evaluation_repetitions=3`. Every repetition
executes the same seeded task instance in a fresh runner workspace and task reset,
with its own cache identity and immutable evidence. U is the observed full-pass
fraction over actual executions (0/1, 1/2, 2/3, or 3/3). Run repetitions
sequentially and stop at the first measured failure. Only preceding passes
admit later repetitions; skipped repetitions are not recorded as failures.
This adaptively stopped fraction is not an unbiased reliability estimate.
Resume reuses completed repetition slots and runs only missing slots. Each slot
retains the existing initial execution plus one infrastructure retry; a retry is
not an extra vote and a measured failure is never resampled. A trusted bounded
agent failure is an observed non-completion even if grading was not reached;
its absent raw reward is preserved. Exceptions raised inside candidate `artemis/`
source during agent execution are also terminal agent failures, with their raise-site
provenance retained and no fabricated AndroidWorld reward. OS/transport exceptions
(including chained transport causes), external raise sites and setup/grading/cleanup
errors remain unavailable infrastructure/evaluator outcomes. Infrastructure-invalid runs remain unavailable
and do not fill a repetition slot. Targeted diagnostics
remain separate, single-run observations and cannot fill canonical slots.

A child qualifies only when every required repetition passes AndroidWorld
(`U=1`), its completed effective judge score is strictly greater than the task
tree's original root judge score, and its review does not confirm manipulation.
A passing root can skip search after all repetitions pass and a usable review;
root self-comparison does not require an impossible strict improvement.
The judge reviews the aggregate evidence once per artifact, including all samples.
`evaluation_repetitions` is a frozen integer setting from 1 to 3 (default 3);
non-single-task evaluation retains one execution per task.
The baseline remains the original root throughout that task. Scores and snapshot
identities are checked against their persisted canonical outcome and judge reports;
missing, partial, or contradictory evidence cannot trigger this rule. A qualifying
passing root needs no search trials. This rule does not apply to multi-task search.

Score publication commits a `task_success_stop` receipt in the same transaction.
New trials and new selections are then blocked, while already admitted work drains
through assessment, transition recording, and exactly-once backup. Interrupted
work stays recovery-pending and prevents finalization; resume settles it before
handoff. A qualifying frontier is an absorbing success state with zero remaining
value. A committed success stops new selections at every frontier in that tree;
frontiers frozen after the stop also have zero remaining value. A forecast frozen
before success retains its original evidence cutoff and normal validation, even
if it finishes during draining. Missing forecasts never receive a synthetic zero.

Finalization may finish below the configured rollout limit only with a validated
success receipt and settled trials/reservations. It chooses the highest observed
combined score among qualifying harnesses, using the existing deterministic ties.
It verifies canonical evidence again before freezing/exporting the winner. Reports
retain the trigger, original root score, actual completed rollout count, configured
limit, and whether stopping was early. The task sequence passes that exact winner
to the next task, where the root is evaluated afresh. Without qualifying success,
the existing configured-budget combined-score selection remains in effect.

Set `stop_on_task_success=false` to run the full configured search for comparison.
The flag, prediction protocol v6, success policy, and prompt library v34 belong to
the frozen experiment identity. Start a fresh compatible run; historical statistics
and already-running experiments are not migrated or silently reinterpreted.
