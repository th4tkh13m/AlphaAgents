<aside>
💡

**Core idea:** The archive may collectively contain the capabilities needed to solve all development tasks even when no single node contains them all. When the current node fails a task, reuse evidence and mechanisms from nodes that already succeed on that task, while preserving the current node's existing capabilities.

</aside>

## Motivation

The task-resolution matrix shows that successful capabilities are **fragmented across nodes**. A later mutation may gain one capability while regressing another, so optimizing only aggregate score can repeatedly rediscover or lose useful behavior.

The goal is therefore not to copy an entire successful harness. It is to identify the **minimal transferable capability** responsible for a success and integrate it into a stronger current node without regression.

## Procedure

For a failed task `t` in current node `n`:

1. **Retrieve** archive nodes that succeed on `t`.
2. **Compare** the current node against those successful nodes:
    - code differences
    - prompts
    - trajectory behavior
    - action outcomes
    - relevant previous hypotheses
3. **Identify the minimal capability difference** likely responsible for success.
4. **Integrate** that capability into the current node rather than replacing the whole harness.
5. **Evaluate** the target task and run regression checks on tasks the current node already solves.
6. **Accept** only if the desired capability is gained without unacceptable capability loss.

## Possible agent strategies

| Strategy | Behavior | Role of archive |
| --- | --- | --- |
| **Hypothesis only** | Analyze the current failure and derive a repair from the current node's code and trajectory. | No archive look-back; baseline condition. |
| **Hypothesis + look-back** | Form a root-cause hypothesis first, then inspect successful archive nodes to support, reject, or refine it. | Archive acts as evidence. |
| **Look-back** | Retrieve successful nodes first, compare them with the current node, and derive the repair from the capability difference. | Archive acts as a solution source. |

## Regression-aware integration

A retrieved node may solve the target task while performing worse elsewhere. Therefore the agent should transfer a **mechanism**, not blindly copy a patch or replace the parent.

A candidate integration should track:

- **Capability gained:** did the previously failed target become solved?
- **Capabilities preserved:** which tasks solved by the parent remain solved?
- **Regressions introduced:** which previously solved tasks become failures?
- **Generalization:** does the mechanism help related or held-out tasks?

## Avoiding benchmaxxing

The development task can be used to identify a failure and retrieve precedents, but the agent should explain the repair at the **capability/mechanism level**, not as a task-specific special case.

Use the Selection set for improvement and regression testing, then measure transfer on held-out Confirmation tasks. A gain only on the exact development tasks is evidence of benchmark specialization rather than general capability transfer.

## Design questions

- Which successful node should be retrieved when multiple nodes solve the same task?
- Should retrieval use exact task success, similar failure mode, or inferred capability?
- What information from the successful node should be exposed to the coding agent?
- How should the agent isolate the causal difference between successful and failing nodes?
- What regression budget is acceptable?
- Should capability-preserving integration be handled by the coding agent itself or enforced by the search controller?
- How should we evaluate **Hypothesis**, **Hypothesis + look-back**, and **Look-back** fairly under the same evaluation budget?