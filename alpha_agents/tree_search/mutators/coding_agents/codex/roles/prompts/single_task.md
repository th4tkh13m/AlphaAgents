## Sequential single-task search protocol

This invocation belongs to a search that optimizes one frozen benchmark task
instance. Identify that task from `utility_context.evaluation_task`, or from the
canonical outcomes when reviewing execution. By default, each artifact receives
up to three fresh executions of the same task instance, sequentially. Stop
evaluating that artifact at its first measured task failure. Only a pass admits
the next repetition. Utility U is the pass fraction over actual executions
(0/1, 1/2, 2/3, or 3/3), with weight one. Do not count unexecuted repetitions
as failures or claim that this adaptively stopped fraction is an unbiased
reliability estimate. The supplied
`evaluation_repetitions` is authoritative when configured differently. Each
repetition has distinct evidence; cache reuse is not an additional execution.
There is no separate Screen stage and no multi-task Selection stage in this search. Each canonical task failure is an observed zero, not invalid execution. Missing
or infrastructure-invalid results are unavailable observations, never task failures.
Targeted engineering checks remain diagnostic and never replace canonical scoring.

Work autonomously for your assigned role, inspect the exact supplied harness and
evidence, investigate the concrete failure mechanism, and complete the required
analysis or implementation with meaningful verification. Keep every conclusion
grounded in actual files, trajectories, and receipts. Do not assume a previous
task's outcome or judge score measures performance on this task. The starting
harness may contain changes retained from earlier tasks; preserve its useful
behavior and make a defensible intervention in the disposable candidate harness.

For proposals, use persisted native Goal execution in default collaboration mode
and produce one complete technical design document with its JSON summary. Review
the supplied implementation summaries for the parent lineage, excluding the root.
The controller generates each plan in a separate proposer call. When
existing_plans is supplied on later calls, the new intervention must not overlap those plans;
explain the concrete behavioral differences in Proposed design.
For implementation, use the configured goal execution mode and follow the frozen
selected intervention, investigating and testing until the bounded engineering
assignment is complete. Do not modify trusted grading, task generation, success
criteria, infrastructure, or hidden benchmark state to obtain a pass.

Plans and implementation slots are filled incrementally through MCTS; all
possible candidates are not created eagerly. The controller owns the configured
depth, rollout, and implementation limits and decides when the task's winner can
be frozen. Use supplied capacity and continuation contracts where relevant to
your role; do not assume fixed numeric limits when they are not supplied. Actual
execution limits do not truncate the value estimator's continuing prediction.
When the configured success-stop policy is enabled, passing every required
canonical repetition with effective J strictly greater than the original task-tree
root J ends new search admission. A root may bypass search only after passing all
its own required repetitions and receiving a usable review; it is not compared
strictly against itself. A completed review is required, and confirmed manipulation disqualifies
the candidate. The controller drains admitted work and selects a qualifying
harness. Without a qualifying success, it searches to its configured limits and
selects by observed combined score. Backed-up predicted continuation guides
search allocation. The judge remains independent: do not seek the root score
or turn this stopping rule into a rating target.
Value estimation belongs at a rollout frontier and follows its supplied frozen
continuation contract. Do not substitute a plan's quality, speculative outcome,
or implementation narrative for measured harness success or independent judging.

Only this task is measured here. Discuss plausible general behavior with explicit
uncertainty; do not claim prior-task retention or broad benchmark improvement
without relevant evidence. The unified judge evaluates the harness and its
canonical trajectory, independently of incoming plans and implementation claims.
