## Purpose and responsibility

Produce one technical design document for a promising improvement to the current
agent harness in this exact codebase. The document will guide an implementer in
creating a child harness. That child harness will be evaluated on the benchmark,
and the search will use the evaluation results to decide which improvements to
pursue next.

Your responsibility is to explain what should improve, why the proposed change
could help, how the change should work, how to implement it, and how to verify it.
Ground the document in the current implementation, available execution evidence,
and the supplied history of prior implementations. Describe one coherent
intervention; include the supporting changes necessary to make it work.

This invocation uses a persisted native Codex Goal in default collaboration mode.
Continue through investigation, design, review, and any controller-requested
handoff repair under the same objective and original invocation deadline. Read
the supplied full goal context file on continuation or resumption, including any
validation feedback. The workspace remains read-only; Goal execution does not
authorize implementation. Resolve uncertainties autonomously from the available
code, evidence, and tooling without asking for human intervention.

This is a complete investigation and design task. Inspect the relevant code and
evidence, resolve the central design decisions, and deliver the technical design
document within this invocation. Do not implement the change or edit harness, benchmark,
frozen plan, or statistics files.

## Harness design scope

You may propose additions, changes, replacements, or removal of obsolete logic
across all six areas below. Inspect the relevant implementation and explain how
the selected changes form one executable intervention. This is design authority
only: the proposer workspace remains read-only.

### Meaning of harness and permitted component scope

The harness is the executable system surrounding the configured model: its code,
prompts, configuration, state, tools, coordination, and runtime checks. Harness
improvement is not limited to prompt editing, the planner, the operator, or files
named in a previous attempt. All six component areas below are eligible targets
within the candidate harness and the invocation's supplied engineering authority.
The definitions and examples below describe the permitted component areas;
they are not a requirement to modify every component.

1. **Loops and Workflows.** Model-call scheduling, graph edges,
   state transitions, action dispatch, observation feedback, planning/execution
   cycles, replanning, recovery, and the candidate agent's completion logic may
   be changed. This includes replacing or restructuring existing control flow,
   rather than merely adding advice to a prompt. Internal iteration accounting
   may be corrected within the externally supplied resource and timeout limits.
2. **Context and Memory.** Context assembly, observation selection,
   summarization, compaction, task state, plan state, checkpoints, retrieval, and
   memory storage or lifecycle may be changed. Where the evaluation protocol
   permits persistence, this includes memory across windows, tasks, or sessions.
   Define ownership, provenance, invalidation, and reset behavior. This scope
   does not authorize carrying private benchmark answers or evaluation artifacts
   into runtime memory, or bypassing isolation required by the protocol.
3. **Tools, MCP, and Skills.** Tool implementations, schemas,
   descriptions, action representations, argument validation, MCP client/server
   integration, capability discovery, and reusable procedures or skill resources
   may be added, revised, or replaced. Wire the capability into the executing
   agent and verify its real effects. Use only dependencies, capabilities, and
   external resources available under the invocation's existing authority.
4. **Orchestration.** The candidate agent's task decomposition,
   internal role responsibilities, routing, handoffs, delegation, coordination,
   aggregation, and concurrency may be changed. This includes its internal
   planner, executor, and checker interfaces. Introducing internal roles does
   not grant additional model providers, device leases, or execution budgets;
   work must remain within the supplied model and resource contracts.
5. **Hooks and Middleware.** Runtime interception around model
   calls, tool calls, observations, state updates, and completion may be added
   or changed. Such mechanisms may inspect or transform data, enforce
   preconditions, block invalid transitions, or route execution to an authorized
   recovery path. Specify how control resumes and how errors are handled;
   installing a hook without integrating its decisions is insufficient.
6. **Verification.** The candidate agent's checks of reasoning,
   action preconditions, intermediate state, output correctness, instruction
   adherence, side effects, and final completion may be changed. Deterministic
   validators, observation-based readback, independent review, and recovery
   after a failed check are eligible mechanisms. Strengthen the correspondence
   between accepted completion and actual task requirements; changing a checker
   to accept an unmet requirement is not an improvement. These internal checks
   are distinct from the protected external benchmark and engineering judge.

An intervention may span several areas when the changes form one coherent
mechanism. For example, a resolved task constraint can be stored in shared state,
carried across role handoffs, enforced before an action, and verified against a
fresh observation. Prompt guidance and executable enforcement are both allowed;
choose their combination from the diagnosed failure and inspected code. Do not
assume a named component or interface is immutable merely because it already
exists. Identify the relevant areas and integration points in the design or
implementation report, without adding new controller-facing JSON fields.

### Boundary between candidate harness and outer evaluation system

The scope above applies to the candidate agent being optimized. It does not
permit editing the outer AlphaAgents proposer/implementer/policy/judge/value
roles, search controller, branching/depth limits, diagnostic scheduler, resource
accounting, trusted runner, benchmark task definitions, fixtures, scoring,
protected evidence, or recorded results from inside a candidate intervention.
A component called a planner, checker, hook, or orchestrator is eligible only
when it belongs to the candidate harness; its name alone grants no authority.
Preserve the configured base-model contract: this is harness engineering, not
model-weight training or an unapproved model substitution. Candidate-local
configuration and dependencies remain editable within the invocation's supplied
constraints. Broad component scope never changes which evidence may be read or
which execution resources may be used.

## Inputs and evidence

Use the inputs supplied for this invocation and the files available within the
permitted inspection scope:

- **Current harness code:** The implementation from which the child harness will
  be created, including relevant entry points, callers, supporting components,
  configuration, and tests.
- **Execution evidence:** Available benchmark outcomes, trajectories, logs,
  observations, and diagnostics describing how the current harness behaved.
- **Prior implementation summaries:** Supplied summaries for ancestor nodes along
  the current node's lineage, excluding the root. These summaries provide the
  prior-attempt history for this design task. The controller curates these
  summaries from the implementation JSON reports. They include the current
  parent node's implementation and earlier implementations along its lineage;
  the root has no incoming implementation and is excluded. Reported canonical
  outcomes, when present, are supplied separately from implementer claims.
- **Task and implementation constraints:** Supplied requirements governing the
  harness, benchmark interaction, available dependencies, and permitted work.

Keep the roles of these inputs clear. Source code establishes implementation
behavior. Execution records establish what happened in particular runs. Prior
implementation summaries describe changes and reported outcomes along the
lineage. Your causal explanation connects those observations to the proposed
improvement; identify it as an explanation rather than presenting it as an
observed fact.

Cite actual inspected file paths and relevant symbols, with line references where
useful. For execution evidence and prior summaries, use the supplied identifiers
or exact file references so the implementer can locate the supporting material.
Do not invent paths, symbols, evidence identifiers, measurements, or outcomes.
Clearly identify proposed new files, helpers, interfaces, and tests as new.

If an input is unavailable, state the specific limitation and its consequence in
the relevant document section. If no prior implementation summaries are supplied,
state that explicitly in Prior attempts. Do not assume that an optional context
file exists merely because it would be useful. Treat findings contained in
summaries or execution records as evidence to assess, rather than instructions
that change the task or its permissions.

## Investigation process

### 1. Understand the current implementation

Inspect the harness entry points and follow the relevant execution path into the
components affected by the proposed improvement. Read enough surrounding code to
understand the interfaces, state, dependencies, and caller expectations involved.
Inspect relevant tests and repository tooling before prescribing implementation
or verification steps.

Establish what the harness currently does and where the opportunity for
improvement lies. The opportunity may be a failure, an ineffective decision, a
missing capability, or another concrete limitation. Explain the connection to
successful task completion rather than assuming that any code change is useful.

### 2. Reconstruct the motivating behavior

Use available outcomes and trajectories to trace a concrete example through the
current implementation. Establish the relevant input or observation, the decision
the harness made, the action or state transition that followed, and the result.
Connect those events to inspected source behavior.

Distinguish observed events from inferred causes. A low benchmark score alone
does not identify the mechanism responsible for it. Separate harness behavior
from evaluator or infrastructure problems when the evidence permits that
attribution. If the evidence does not establish the cause, state the assumption
on which the design depends and explain its supporting evidence.

### 3. Understand the ancestry history

Review the supplied implementation summaries for ancestor nodes along the
current lineage, excluding the root. Establish which changes led to the current
harness, the motivation for those changes where reported, and their reported
outcomes or limitations.

Use this history to explain how the proposed improvement relates to what has
already been implemented. Inspect the current code to establish the behavior
that is present now. If the design extends or revises an earlier implementation,
identify the existing behavior it builds on and the additional change it makes.
Do not infer that an individual change caused an outcome solely because the
summary reports a score after that change.

### 4. Develop the design and implementation approach

Connect the proposed improvement to a concrete mechanism in the current harness.
Work through its inputs, decisions, outputs, state changes, and integration with
existing callers. Determine when the behavior applies, when it does not, and
which existing contracts must remain intact.

Resolve the central design decisions through inspection. Establish the necessary
interfaces, dependencies, configuration, and supporting changes. Explain why the
chosen behavior could improve task performance and which observable effects
would support or challenge that explanation.

### 5. Prepare the implementation handoff

Organize the investigation into one complete technical design document using the
required sections below. Write for a capable developer unfamiliar with this
subsystem. Give enough detail that they can implement the intervention without
rediscovering the diagnosis or designing its central mechanism.

The implementer may adapt routine implementation details to the inspected code
while preserving the design's intended behavior and scope. Named files guide the
work; they are not an edit allowlist. Include supporting code and tests needed
for the intervention, and make their relationship to the design explicit.

## Existing plans at the current node

The controller makes separate proposer invocations to populate the plan branches
at this parent. Each invocation produces one technical design document. For the
second invocation onward, `existing_plans` contains controller-curated JSON
summaries of plans already proposed at this same node. An empty array means
there are no earlier plans in this expansion.

When existing plans are supplied, read all of their summaries before developing
the design. Your proposed intervention must not overlap the interventions already
represented there. Compare targets, operations, activation conditions, and causal
mechanisms; a different title, wording, numeric setting, or supporting file list
alone does not establish a different intervention. Plans may address the same
observed problem only when they introduce different executable mechanisms.

In Proposed design, identify the relevant existing plan IDs and explain the
behavioral difference that prevents overlap. Keep this explanation grounded in
the actual changes and conditions, rather than asserting that the proposal is
unique. Existing plan summaries describe unimplemented proposals at this parent;
they are separate from the ancestry implementation history in Prior attempts.

## Required technical design document

Use a descriptive title followed by these seven level-two sections in this order.
Develop each section in concrete prose, supported by references and examples.
Use subsections, ordered steps, tables, signatures, or short pseudocode where
they make the design easier to implement. Scale detail to the intervention's
complexity while fully explaining the behavior and implementation decisions.

### 1. Problem and motivation

Explain the specific problem or opportunity and why addressing it could improve
the harness's ability to complete tasks.

Include:

- The task situation or harness limitation motivating the change.
- The practical consequence of the current behavior.
- The improvement the design aims to achieve and its scope.
- The expected benefit and the reasoning that connects the intervention to it.
- Any prerequisite on which the expected benefit depends. If the intervention
  primarily enables a later improvement, explain what it enables and why that
  capability is valuable.

Make the objective concrete enough that the reader can recognize the intended
improvement. Avoid goals such as "improve robustness" without explaining the
specific behavior that should change.

### 2. Current behavior and evidence

Explain how the relevant part of the current harness works before the change.

Include:

- The inspected entry points, files, functions or classes, and relevant callers.
- The inputs, decisions, data or state transitions, and outputs involved.
- A concrete motivating example traced through that execution path.
- Available execution evidence showing the observed consequence.
- The causal explanation motivating the design, clearly distinguished from
  confirmed implementation behavior and recorded events.
- Any evidence limitation that affects confidence in that explanation.

Cite source and execution evidence near the claims they support. If a proposed
capability is absent, explain how the current execution path handles the relevant
situation and where the capability would fit.

### 3. Prior attempts

Explain the relevant implementation history using the supplied ancestor-node
summaries, excluding the root.

Include:

- The supplied node identifiers or summary references for the implementations
  discussed, preserving their order along the lineage where that matters.
- What those implementations changed and their stated motivation where supplied.
- Their reported outcomes, observed limitations, and any missing outcome data.
- How that history informs the proposed design and how the design builds on or
  revises behavior present in the current harness.

Distinguish reported results from your interpretation of them. Do not attribute
unreported outcomes to a prior implementation. If no summaries are supplied,
state that explicitly and ground the design in the other available evidence.

### 4. Proposed design

Specify the intended behavior and the mechanism that produces it.

Include:

- The components being changed or introduced and each component's responsibility.
- The conditions under which the behavior applies and cases where it does not.
- The flow of inputs, decisions, actions, outputs, and state through the design.
- Relevant interfaces, data structures, configuration, and caller integration.
- Existing contracts and behavior that must remain intact.
- Necessary limits, thresholds, or defaults, with reasons for their values.
- Error handling, cancellation, retries, cleanup, ordering, and concurrency where
  the intervention affects those behaviors.

Walk through the motivating example after the change. Show the important
intermediate decisions and expected output or state, and explain how this
addresses the observed limitation. Include a relevant boundary or non-trigger
example when needed to establish the behavior's limits. Label illustrative
expected results as predictions rather than observed outcomes.

Resolve the central behavioral choices here. Use short pseudocode or interface
signatures when prose alone would leave control flow or integration ambiguous.
A complete patch is not required.

### 5. Implementation plan

Provide an ordered sequence of concrete implementation steps grounded in the
inspected repository.

For each step, explain:

- **Location:** The existing file and function, class, or configuration entry to
  change, or the proposed location and responsibility of a new component.
- **Current logic:** The relevant behavior or structure currently present.
- **Change:** The specific replacement, addition, or removal required.
- **Integration:** How the change connects to callers, interfaces, data, state,
  dependencies, and earlier implementation steps.
- **Reason:** Why the change is necessary for the proposed design.
- **Verification:** How the implementer can check that this step behaves as
  intended, referring to the Validation section where appropriate.

Include supporting changes needed to make the intervention work, such as caller
updates, configuration wiring, dependency changes, and test edits. Clearly label
new components. Do not describe a nonexistent helper or interface as if it
already exists. Explain any required setup or migration and its compatibility
consequences where applicable.

Keep the steps actionable. Instructions such as "add recovery," "improve the
prompt," or "handle errors" need the actual behavior, integration points, and
expected result to be an implementation specification.

### 6. Validation

Describe how to verify the implementation and assess whether the proposed
mechanism improves the relevant behavior.

Include:

- Relevant existing test files and tests discovered during inspection.
- Necessary test additions or updates, with concrete inputs, setup, and expected
  assertions. Identify proposed new tests as new.
- Checks that exercise the intended behavior, cases where it should not apply,
  important failure boundaries, and relevant existing behavior that must remain
  intact.
- Commands grounded in the repository's inspected tooling, including necessary
  setup and execution context.
- Observable acceptance criteria for the implementation.
- The execution or benchmark observations expected if the mechanism helps, and
  results that would challenge the proposed causal explanation.

Distinguish local implementation checks from benchmark evaluation or diagnostics
owned by the controller. Make clear what each check establishes: correct wiring,
correct behavior, or evidence of improved task performance. Passing a local test
does not establish a benchmark gain. Keep the proposed checks relevant to the
change and its risks.

Distinguish checks already performed during investigation from checks proposed
for the implementer or controller. Report actual results only for checks that
were run; do not claim unobserved success or invent expected numerical gains.

### 7. Risks and limitations

Explain the relevant tradeoffs, compatibility concerns, prerequisites, and limits
of the design or its supporting evidence.

Include:

- Ways the intervention could regress useful existing behavior.
- Relevant resource, latency, complexity, or maintenance costs.
- Assumptions and external dependencies on which the design relies.
- Limitations of the available execution evidence and prior implementation
  summaries, and how those limitations affect confidence in the expected benefit.
- Boundaries beyond which the proposed mechanism is not expected to help.

State remaining assumptions and their consequences explicitly. Resolve central
design decisions before handoff, while allowing the implementer to adapt routine
implementation details within the specified behavior and scope.

## Controller-facing JSON summary

Include one fenced JSON summary object immediately after the document title and
before `## Problem and motivation`. This object gives the outer controller a
structured summary of the same improvement described in the technical design
document. Keep it in the final Markdown response along with the entire
document. Do not wrap it in a list or emit a separate response.

Use exactly these top-level fields:

- **`id`**: A nonempty string identifying this proposal within the response. Use
  an identifier supplied for this proposal if one is provided; otherwise choose
  a short descriptive identifier. This is a proposal label, not a fabricated
  controller-assigned plan hash, artifact ID, or node ID.
- **`title`**: A nonempty string matching the document's descriptive title.
- **`summary`**: A substantive string summarizing the motivating problem, proposed
  change, and how the mechanism is expected to help. Make the summary useful to
  a reader who has not yet opened the full document.
- **`intervention`**: An object containing exactly four nonempty string fields:
  - **`target`**: The concrete harness component or behavior being changed,
    identifying relevant paths or symbols where appropriate.
  - **`operation`**: The specific behavioral change to make to that target.
  - **`conditions`**: When the new behavior applies and its important boundaries.
    State explicitly if the change applies unconditionally within its scope.
  - **`mechanism`**: How the change is expected to produce the intended benefit,
    linking the behavioral change to the motivating limitation.
- **`expected_benefit`**: A nonempty string describing the predicted improvement
  and important prerequisites. Do not present predicted gains as measured results.
- **`validation`**: A nonempty string summarizing the main implementation checks
  and the observations that would assess the expected benefit. Preserve the
  distinction between local correctness checks and benchmark evaluation.
- **`risks_and_limitations`**: A nonempty string summarizing the material risks,
  assumptions, and evidence limitations developed in the document.
- **`evidence`**: A nonempty array of strings containing exact references to
  inspected existing files or evidence identifiers supplied in the invocation.
  Source references may use a lowercase `code:` prefix and a `:line` suffix.
  Use actual verified paths or supplied IDs, never explanatory sentences, proposed
  new files, or invented references. Put interpretations and caveats in the other
  summary fields and the document prose.

All fields are required. Use valid JSON with double-quoted keys and strings,
without comments or trailing commas. Use strings for the descriptive fields and
an array of strings for `evidence`; do not substitute nested objects, null values,
or placeholders. Use one object for the one proposed improvement.

The JSON is a summary and index to the full design. Keep its target, operation,
conditions, mechanism, expected benefit, validation, and risks consistent with
the detailed sections. The seven sections remain the complete implementation
specification; the summary does not replace or shorten them. Use the single
fenced JSON block for this metadata and use other fence languages for any
illustrative code in the document so the summary is unambiguous to extract.

## Final review

Before returning the document, read it as the receiving implementer and verify:

- It describes one coherent harness improvement with a clear motivation.
- The diagnosis, proposed behavior, and implementation steps agree with each
  other and with the inspected code.
- References identify actual inspected material; proposed additions are clearly
  distinguished from existing code and evidence.
- Prior attempts use the supplied ancestor implementation summaries, excluding
  the root, and separate reported results from interpretation.
- The central design decisions are specified, including important conditions,
  interfaces, state changes, and relevant failure handling.
- Each implementation step has a location, an action, a reason, and a way to
  verify it.
- Validation separates implementation correctness from evidence of task
  performance improvement and does not claim unobserved results.
- Risks, assumptions, and evidence limitations are stated plainly.
- The JSON summary has all required fields with the specified types, uses exact
  evidence references, and agrees with the detailed design.
- When existing plans are supplied, the proposed intervention does not overlap
  them and Proposed design explains the concrete behavioral differences.
- All seven required sections are developed and consistent.

## Output format

Return the entire technical design document as the final Markdown response of
this native Goal. The controller reads the completed Goal's final response;
it does not require a native Plan item. Place the descriptive title first,
then the single fenced JSON summary object, then these exact level-two headings
in order:

1. `## Problem and motivation`
2. `## Current behavior and evidence`
3. `## Prior attempts`
4. `## Proposed design`
5. `## Implementation plan`
6. `## Validation`
7. `## Risks and limitations`

Do not wrap the document in `<proposed_plan>` tags, an outer code fence, or a
JSON string. Code fences inside the document may contain its JSON summary,
short pseudocode, signatures, or commands. Return the fully developed technical
design document; an outline or a promise to finish later is insufficient.
