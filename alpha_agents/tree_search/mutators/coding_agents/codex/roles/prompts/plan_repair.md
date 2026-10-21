# Repair the technical design document within this invocation

## Purpose and scope

Your previous native Goal handoff completed, but its technical design document
failed the controller's parsing or validation. The validation feedback and
complete original request appear below. Continue in the same persisted planning
thread and resume the same native Goal in default collaboration mode to repair
the handoff. The controller has placed this feedback in your goal context file;
reread it on resumption. Preserve the original Goal objective and accounting.

The original proposer task remains governing: produce one complete technical
design document for improving the current agent harness, accompanied by one
controller-facing JSON summary. Preserve the original task's artifact identity,
inputs, permissions, evidence scope, and implementation constraints. This repair
does not authorize implementation or edits to harness, benchmark, frozen plan,
statistics, or other roles' files.

Use the investigation and useful design material already developed. Repair the
same proposed improvement, preserving its motivation and intended behavior while
correcting the defects identified in the handoff. Resolve design ambiguities
through permitted inspection and make the corrected specification concrete.

The original invocation deadline and remaining time still apply. A repair turn
does not reset the deadline or create a new planning invocation. Use the actual
remaining time reported in the validation feedback.

## Repair process

1. **Diagnose the reported failure.** Read the exact validation feedback and
   locate the affected document content or JSON fields. Determine whether the
   issue concerns formatting, JSON syntax or types, missing content, invalid
   references, or inconsistency between the summary and the detailed design.

2. **Check the governing output contract.** Compare the previous response with
   the original proposer instructions and the requirements below. Correct the
   underlying issue rather than merely changing wording around it. Do not add
   fabricated references, measurements, or outcomes to satisfy a validator.

3. **Correct all affected content.** If a correction changes a design detail,
   update the related explanation, implementation steps, validation, risks, and
   JSON summary together. When `existing_plans` is supplied, preserve the
   requirement that this intervention does not overlap those plans and explain
   its concrete behavioral differences in Proposed design. Preserve the substantive implementation specification;
   a syntactically valid summary cannot replace a developed design document.

4. **Verify evidence and history.** Check cited source paths, symbols, execution
   records, and supplied summary identifiers within the permitted scope. Prior
   attempts use the supplied implementation summaries for ancestor nodes along
   the current lineage, excluding the root. Separate their reported outcomes
   from your interpretation. Use the controller-curated implementation JSON
   summaries, including the current parent implementation and excluding the root.
   State actual missing evidence and its consequences
   in the relevant section without inventing unavailable material.

5. **Review and return the complete handoff.** Check the entire corrected
   document, including requirements beyond the first reported error. Return one
   complete technical design document with its JSON summary and all seven
   developed sections, even if the repair affects only a small part of it.

## Controller-facing JSON summary

Place exactly one fenced `json` object immediately after the document title and
before `## Problem and motivation`, in the complete final Markdown response.
Use exactly the following top-level fields, all required:

- **`id`**: A nonempty string identifying the proposal. Preserve the proposal
  identifier from the original handoff unless correcting an identifier error
  under the original request. Use a supplied proposal identifier when provided;
  otherwise use a short descriptive label. Do not invent controller-assigned
  plan hashes, artifact IDs, or node IDs.
- **`title`**: A nonempty string matching the document's descriptive title.
- **`summary`**: A substantive string explaining the motivating problem, proposed
  change, and mechanism by which it is expected to help.
- **`intervention`**: An object with exactly four nonempty string fields:
  - **`target`**: The concrete component or behavior being changed, identifying
    relevant paths or symbols where appropriate.
  - **`operation`**: The specific behavioral change to make.
  - **`conditions`**: When the change applies and its important boundaries.
    Explicitly state if it applies unconditionally within its scope.
  - **`mechanism`**: How the change is expected to address the motivating
    limitation and produce the intended benefit.
- **`expected_benefit`**: A nonempty string describing the predicted improvement
  and important prerequisites, without presenting predictions as measured gains.
- **`validation`**: A nonempty string summarizing the implementation checks and
  observations that would assess the benefit. Distinguish local correctness
  checks from benchmark evaluation.
- **`risks_and_limitations`**: A nonempty string summarizing material risks,
  assumptions, and evidence limitations explained in the document.
- **`evidence`**: A nonempty array of strings naming exact inspected existing
  files or evidence identifiers supplied in the invocation. Source references
  may use a lowercase `code:` prefix and a `:line` suffix. Do not put prose,
  proposed new files, abbreviated identifiers, or invented paths in this array.

Use valid JSON with double-quoted keys and strings, without comments, trailing
commas, nulls, or placeholders. Apart from the `intervention` object and `evidence`
array, all top-level values are strings. Keep causal explanations, assumptions,
and missing-evidence caveats in the descriptive fields and detailed prose.

The JSON summarizes the same improvement as the document. A correction to one
must be reflected in the other wherever it changes their meaning. Use the one
fenced JSON block for this metadata; use other fence languages for illustrative
code so the controller-facing summary is unambiguous to extract.

## Technical design document requirements

Preserve and develop these exact level-two sections in order:

1. **`## Problem and motivation`**: Explain the specific problem or opportunity,
   its consequence for task completion, the intended improvement, and its scope.
   Explain expected benefits and important prerequisites.

2. **`## Current behavior and evidence`**: Explain the relevant inspected files,
   symbols, callers, interfaces, and state transitions. Trace a concrete example
   through the current execution path. Cite available execution evidence and
   separate observed behavior from the proposed causal explanation. State
   evidence limitations that affect confidence in the diagnosis.

3. **`## Prior attempts`**: Explain the supplied ancestor implementation summaries,
   excluding the root. Identify the changes and reported outcomes relevant to
   the design, and explain how the proposed change builds on or revises the
   current implementation. Use exact supplied references. If no summaries were
   supplied, state that explicitly.

4. **`## Proposed design`**: Specify components, activation conditions, inputs,
   decisions, outputs, state changes, interfaces, and caller integration.
   Explain contracts that must remain intact and justify necessary limits or
   defaults. Address relevant failure handling, cancellation, retries, cleanup,
   ordering, and concurrency. Walk through the motivating example after the
   change, showing important intermediate decisions and the expected result.
   Include a boundary or non-trigger example where needed. Resolve central
   behavioral choices; label predictions as expected rather than observed.

5. **`## Implementation plan`**: Give ordered, concrete edits grounded in the
   inspected repository. Each step needs a location, current logic, required
   change, integration with callers or state, a reason, and a way to verify it.
   Include necessary supporting changes and test edits. Clearly identify new
   components. Named files guide implementation and are not an edit allowlist;
   the implementer may adapt routine details while preserving intended behavior
   and scope.

6. **`## Validation`**: Identify relevant existing tests, necessary additions or
   updates, concrete setups, and expected assertions. Cover intended behavior,
   non-trigger cases, important failure boundaries, and relevant regressions.
   Ground commands in inspected repository tooling. Separate local checks from
   controller-owned benchmark evaluation or diagnostics. Explain acceptance
   criteria and observations that would support or challenge the mechanism.
   Distinguish checks actually run from proposed checks; do not claim unobserved
   success or numerical performance gains.

7. **`## Risks and limitations`**: Explain compatibility concerns, tradeoffs,
   possible regressions, relevant costs, prerequisites, assumptions, evidence
   limitations, and the boundaries of the expected benefit. State consequences
   of remaining assumptions explicitly. Resolve central design decisions before
   handing the document to the implementer.

## Final review and output

Before returning the corrected document, verify that:

- It specifies one coherent harness improvement and preserves the engineering
  substance of the investigation.
- The JSON summary has exactly the required fields and types and agrees with
  the title, diagnosis, design, implementation, validation, and risks.
- Every evidence reference identifies inspected existing material or an exact
  supplied evidence identifier. Proposed additions are clearly labeled as new.
- Prior attempts reflect the supplied ancestry summaries, excluding the root,
  with reported outcomes distinguished from causal interpretation.
- All seven document sections are complete, correctly named, and in order.
- Each implementation step is actionable and has a way to check its behavior.
- Code fences and JSON syntax are valid and closed.
- The reported validation defects and any related inconsistencies are corrected.

Return the entire corrected document as the final Markdown response of the same
native Goal. Put the descriptive title first, then the single fenced JSON
summary object, then the seven developed sections. Do not use `<proposed_plan>`
tags, an outer code fence, or an outer JSON wrapper. The controller consumes the
completed Goal's final response rather than a native Plan item.

A partial patch, isolated JSON fragment, outline, or ordinary agent-message
summary is not the completed handoff. Repair the full document while retaining
its inspected evidence, concrete design, implementation instructions, and
meaningful validation.

## Trusted validation data

[[VALIDATION_ERROR_JSON]]

## Complete original request

[[ORIGINAL_REQUEST]]
