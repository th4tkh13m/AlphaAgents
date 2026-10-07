> **Idea:** Replace one-shot patch generation with an explicit search over competing causal hypotheses. The coding agent proposes hypotheses and interventions; a tree-search controller allocates evaluation budget, expands promising branches, and propagates downstream evidence back to earlier hypotheses.
> 

## Motivation

Current harness optimization often behaves as:

```
parent harness → coding agent → one patch → evaluate → keep/discard
```

This conflates three decisions:

1. **What failure should be investigated?**
2. **What causal hypothesis explains it?**
3. **What code change should test or repair that hypothesis?**

Different coding agents can inspect different evidence, form different explanations, and produce different repairs. Instead of treating this divergence as uncontrolled sampling noise, make the competing hypotheses explicit and search over them.

The proposed structure is:

```
failure evidence
    ↓
candidate hypotheses
    ↓
hypothesis-conditioned interventions
    ↓
child harnesses
    ↓
evaluation
    ↓
backpropagated evidence
    ↓
next hypothesis/branch selection
```

The central principle is:

> **Search over competing causal hypotheses, not only over generated patches.**
> 

## AlphaGo analogy

| AlphaGo | AlphaAgents |
| --- | --- |
| Current board state | Current harness + unresolved failures + accumulated evidence |
| Candidate move | Candidate hypothesis/intervention |
| Policy prior $P(a\mid s)$ | LLM prior over plausible hypotheses |
| Child board position | Modified child harness + new evidence |
| Value estimate | Expected utility/potential of pursuing a branch |
| MCTS expansion | Instantiate one hypothesis as a concrete intervention and child harness |
| Rollout/evaluation | Run diagnostic probes + benchmark tasks |
| Backup | Propagate downstream improvement/regression evidence to selected ancestors |

The important analogy is the separation between **prior plausibility** and **empirical search value**. A low-prior hypothesis can become preferred if descendants generated from it consistently produce useful improvements.

## Search state

Let each search node be

$$
n_i = (H_i, F_i, E_i, \mathcal{B}_i, R_i)
$$

where:

- $H_i$: concrete harness version.
- $F_i$: unresolved failures or target behaviors.
- $E_i$: accumulated evidence, including trajectories, probes, diffs, task outcomes, and regressions.
- $\mathcal{B}_i$: explicit belief state over candidate causal hypotheses.
- $R_i$: evaluation record and cost.

This makes the node an **epistemic state**, not merely a code snapshot.

A node should preserve at least:

- hypotheses considered,
- evidence supporting/contradicting each hypothesis,
- interventions already attempted,
- predicted behavior change,
- observed outcome,
- unresolved questions.

## Search action

An action should not be "generate arbitrary child." Define it as a **hypothesis-conditioned intervention**:

$$
a = (h, I)
$$

where:

- $h$: causal hypothesis, e.g. "repetition is caused by missing escalation."
- $I$: intervention intended to test or repair $h$, e.g. "after repeated equivalent actions, mark the attempt failed and replan."

The coding agent then instantiates $I$ as a concrete patch, producing a child harness $H'$.

This distinction matters because:

$$
\text{hypothesis} \neq \text{intervention} \neq \text{generated code}
$$

A child can therefore fail because the hypothesis was wrong, because the intervention was insufficient, or because the implementation did not faithfully realize the intervention.

## Hypothesis generation / policy prior

Given node $n$, ask the coding agent to produce a small set of competing hypotheses:

$$
\mathcal{H}(n)=\{h_1,\ldots,h_K\}
$$

For each hypothesis, record:

- causal claim,
- supporting evidence,
- contradictory evidence,
- discriminating probe,
- proposed intervention,
- predicted outcome,
- confidence/prior.

The model supplies a prior:

$$
P(h\mid n)
$$

This is analogous to AlphaGo's policy prior: it determines where search should initially spend computation, but it should **not** determine the final branch by itself.

## Keep two quantities separate

### 1. Hypothesis plausibility

$$
B(h)=P(h\text{ explains the failure}\mid E)
$$

This represents belief in the causal explanation.

### 2. Search value

$$
Q(n,h)=\mathbb{E}[\text{future improvement utility}\mid n,h]
$$

This represents how valuable it has been to pursue the hypothesis branch.

These should not be conflated. A hypothesis may be unlikely as a root cause but still lead to a useful intervention; conversely, a plausible diagnosis may produce little transferable improvement.

## Tree policy

A PUCT-style selection rule can combine empirical branch value with the LLM prior:

$$
h^*=\arg\max_h\left[
Q(n,h)+c\,P(h\mid n)\frac{\sqrt{N(n)}}{1+N(n,h)}
\right]
$$

where:

- $Q(n,h)$: empirical value of pursuing hypothesis $h$,
- $P(h\mid n)$: prior plausibility/promisingness from the coding agent,
- $N(n)$: number of visits/evaluations under node $n$,
- $N(n,h)$: number of expansions/evaluations allocated to hypothesis $h$,
- $c$: exploration coefficient.

This gives the search an AlphaGo-like behavior:

- high-prior hypotheses are examined earlier,
- underexplored alternatives still receive budget,
- empirical success can override the original prior.

## Expansion

When the tree policy selects an unexpanded hypothesis $h$:

1. Choose or generate a discriminating intervention $I$.
2. Ask the coding agent to implement $I$ as a focused patch.
3. Verify **reasoning–implementation alignment**:
    - Does the diff actually implement the stated intervention?
    - Did the changed path execute?
    - Were unrelated edits introduced?
4. Produce child node $n'$ with the modified harness and updated evidence.

An edge should therefore preserve:

$$
n \xrightarrow{(h,I,\Delta H)} n'
$$

rather than only parent → child code provenance.

## Evaluation

Evaluation can have two stages:

1. **Diagnostic probe:** cheap test designed to discriminate the selected hypothesis.
2. **Harness evaluation:** screen/selection/confirmation tasks to measure actual improvement and regression.

A reward may combine:

$$
r = w_1\Delta\text{TaskSuccess}
-w_2\text{Regression}
+w_3\text{ProbeInformation}
-w_4\text{Cost}
$$

The exact reward should remain task-level and interpretable rather than collapsing all evidence prematurely into one scalar.

## Backup

After exploring a path

$$
n_0 \xrightarrow{h_0} n_1 \xrightarrow{h_1} \cdots \xrightarrow{h_{L-1}} n_L
$$

the observed downstream return is propagated **only along the selected path**.

For ordinary MCTS-style empirical backup:

$$
Q(n,h) \leftarrow Q(n,h)
+\frac{G-Q(n,h)}{N(n,h)}
$$

where $G$ is the observed downstream return.

This updates the value of **pursuing the hypothesis branch**, not the probability that the hypothesis is logically true.

### Optional TD-style extension

The method should only be called TD-style if values are bootstrapped from successor estimates. For example:

$$
\delta_t=r_t+\gamma V(n_{t+1})-V(n_t)
$$

$$
V(n_t)\leftarrow V(n_t)+\alpha\delta_t
$$

This is useful if AlphaAgents learns or maintains a value estimator for partially explored harness states. Without bootstrapping, the simpler description is **MCTS-style hypothesis search with empirical backup**, not TD-MCTS.

## Belief update

Separately from search-value backup, evaluation evidence should revise the explicit hypothesis belief state.

For example:

```
Before probe:
P(grounding)   = 0.55
P(escalation)  = 0.30
P(state-loss)  = 0.15

Observed:
target coordinates are correct;
repeated action persists after unchanged UI.

After probe:
P(grounding)   ↓
P(escalation)  ↑
P(state-loss)  ≈
```

A Bayesian update is conceptually clean:

$$
P(h\mid E') \propto P(E'\mid h)P(h\mid E)
$$

but in practice the likelihood may be supplied by a structured judge/agent rather than a calibrated probabilistic model. The key requirement is to keep the belief revision explicit and auditable.

## Example

Observed failure:

> The agent repeatedly executes the same action pattern without making progress.
> 

Initial competing hypotheses:

- $h_1$: grounding coordinates are systematically wrong.
- $h_2$: repeated failure is detected but there is no escalation mechanism.
- $h_3$: the harness fails to recognize that the UI state did not change.
- $h_4$: recovery exists but is never triggered because the repetition signature is too specific.

Possible tree:

```
Parent H0
├── h1: grounding
│   └── targeted coordinate patch → H1
├── h2: escalation
│   └── mark failure + replan → H2
│       ├── h2.1: threshold too late → H4
│       └── h2.2: replanning lacks failure context → H5
├── h3: state-change detection
│   └── compare before/after UI state → H3
└── h4: repetition signature
    └── generalize repetition detector → H6
```

Suppose $h_2$ has only moderate prior probability, but H2 and its descendants repeatedly improve multiple tasks without regression. Search value $Q(H_0,h_2)$ increases, causing the controller to allocate more budget to that branch even if another hypothesis had the highest initial prior.

## Relationship to the two AlphaAgents RQs

### RQ1 — Global search policy

At the outer level, AlphaAgents still decides:

> Which harness/branch in the overall archive should receive the next improvement budget?
> 

This operates over the global improvement tree/archive.

### RQ2 — Child improvement policy

Inside a selected parent, hypothesis-guided tree search decides:

> Which failure explanation and intervention should be investigated next?
> 

This creates a **local deliberative search process** within child improvement.

The two levels are therefore:

$$
\text{Global search over harness lineages}
\quad + \quad
\text{Local search over causal hypotheses/interventions}
$$

A practical design question is whether these should remain nested or eventually be unified into one search graph.

## Key distinction from current DGM-style mutation

Current style:

```
select parent → ask coding agent for a mutation → evaluate child
```

Proposed style:

```
select parent
→ expose failure evidence
→ enumerate competing hypotheses
→ assign priors
→ select hypothesis using search statistics
→ implement a discriminating intervention
→ evaluate
→ update hypothesis beliefs + branch value
→ choose next expansion
```

The proposal therefore turns child generation from **sampling patches** into **persistent evidence-driven deliberation**.

## Main research questions

1. Does explicit hypothesis search produce more confirmed improvements per evaluation than direct patch sampling?
2. Does maintaining competing explanations reduce repeated or overfit repairs?
3. Can downstream task outcomes provide useful credit to earlier hypothesis choices?
4. How should prior plausibility $P(h\mid n)$ and empirical search value $Q(n,h)$ be combined?
5. How many hypotheses should be generated before search becomes too expensive?
6. Should hypotheses be regenerated after every new observation or maintained/revised persistently?
7. When do two hypothesis branches represent the same underlying mechanism and should be merged?
8. Does a TD/value model improve allocation beyond empirical MCTS backup?

## Experimental design

Compare at matched generation/evaluation budgets:

| Method | Description |
| --- | --- |
| Direct mutation baseline | One coding-agent repair per selected parent; no explicit competing hypotheses |
| Multi-hypothesis sampling | Generate $K$ hypotheses and evaluate each once; no search/backprop |
| Hypothesis MCTS | LLM hypothesis priors + PUCT allocation + empirical backup |
| Hypothesis MCTS + learned value / TD | Add bootstrapped value estimation for partially explored branches |

Primary measurements:

- confirmed improvement per valid evaluation,
- cost/time to first confirmed improvement,
- regression rate,
- number of repeated hypotheses/repairs,
- task transfer across failures/apps,
- hypothesis diversity,
- prior-vs-outcome calibration,
- hypothesis-to-diff alignment,
- fraction of search budget spent on eventually productive branches.

## Important complications

- **Non-repeatable actions:** the same high-level hypothesis can produce different patches. Store the concrete intervention and artifact version on every edge.
- **Hypothesis truth vs intervention utility:** benchmark improvement does not prove the causal hypothesis is true.
- **Patch confounding:** bundled edits weaken credit assignment; prefer focused diffs when validating a hypothesis.
- **Noisy evaluation:** matched task sets/seeds and confirmation runs are needed before large value updates.
- **Transpositions:** semantically equivalent hypotheses or near-equivalent patches may appear under different wording or parents.
- **Overfitting:** a branch can improve the targeted task while degrading broader capability.
- **Expensive branching:** hypothesis generation itself is cheap relative to evaluation, but evaluating too many branches defeats the purpose of search.

## Working hypothesis

> **Explicitly representing competing causal hypotheses and using tree search to allocate experiments will improve sample efficiency and credit assignment compared with directly sampling code mutations.**
> 

The most conservative first implementation is **PUCT-guided hypothesis search with empirical MCTS backup**. Add TD/value learning only after there is evidence that a bootstrapped value estimator is reliable enough to improve allocation.