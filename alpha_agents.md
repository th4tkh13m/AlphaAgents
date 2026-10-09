> **Idea:** Replace one-shot patch generation with an explicit search over **improvement hypotheses**: structured hypotheses about **what the coding agent should change, why that change should help, and what observable effect it should produce**. A PUCT-style controller allocates experiments among these hypotheses and backpropagates empirical improvement evidence.
> 

## Core formulation

Current harness optimization often behaves as:

```
parent harness → coding agent → patch → evaluate → keep/discard
```

The proposed process is:

```
parent harness + failure evidence
        ↓
generate candidate improvement hypotheses
        ↓
PUCT selects what to try
        ↓
coding agent implements selected hypothesis
        ↓
exact child harness
        ↓
screen + benchmark evaluation
        ↓
update beliefs and search values
        ↓
backpropagate along selected path
        ↓
choose next expansion
```

The central principle is:

> **Search over explicit hypotheses about what the coding agent should do, rather than directly sampling arbitrary patches.**
> 

## What is an improvement hypothesis?

An MCTS action is an **improvement / patch-action hypothesis**:

$$
a=(F,C,M,O)
$$

where:

- $F$: **target failure/problem**,
- $C$: **causal explanation** for the failure,
- $M$: **proposed modification**,
- $O$: **predicted observable outcome** if the hypothesis is correct and the modification is implemented properly.

Example:

> **Failure:** the agent repeatedly taps without progress.
> 

> **Cause:** repetition is detected, but the harness has no escalation mechanism.
> 

> **Modification:** after repeated equivalent unsuccessful actions, mark the attempt failed and invoke replanning with failure context.
> 

> **Expected outcome:** repeated-action loops terminate and the planner chooses a different strategy.
> 

This object is both **scientifically interpretable** and **directly executable**.

It is important to distinguish:

$$
\text{improvement hypothesis}
\neq
\text{generated patch}
\neq
\text{observed effect}
$$

The hypothesis specifies what should be changed and why. The coding agent instantiates it as code. Evaluation then determines what actually happened.

## AlphaGo analogy

| AlphaGo | AlphaAgents |
| --- | --- |
| State $s$ | Harness + failures + accumulated evidence |
| Legal/candidate move $a$ | Improvement hypothesis: what the coding agent should change and why |
| Policy prior $P(a\mid s)$ | LLM prior over promising improvement hypotheses |
| Execute move | Coding agent implements the selected hypothesis |
| Next state $s'$ | Exact modified child harness + new observations |
| Value / rollout | Screening, diagnostic probes, and benchmark evaluation |
| $Q(s,a)$ | Empirical value of pursuing that improvement direction |
| MCTS backup | Propagate downstream improvement evidence along the explored lineage |

The key AlphaGo-like property is:

$$
\text{initial plausibility} \neq \text{empirical search value}
$$

A low-prior improvement hypothesis can become preferred if its descendants repeatedly produce useful improvements.

## Search state

Let each search node be

$$
n_i=(H_i,F_i,E_i,\mathcal{B}_i,R_i)
$$

where:

- $H_i$: exact harness version,
- $F_i$: unresolved failures / target behaviors,
- $E_i$: trajectories, probes, diffs, task outcomes, regressions, and other accumulated evidence,
- $\mathcal{B}_i$: explicit beliefs about relevant causal claims,
- $R_i$: evaluation records, costs, and search statistics.

The node is therefore an **epistemic state**, not merely a code snapshot.

Each node should preserve:

- improvement hypotheses already considered,
- evidence supporting or contradicting their causal claims,
- concrete implementations already generated,
- predicted behavioral effects,
- observed effects,
- unresolved failures and questions.

## Candidate-action generation

Given a node $n$, the coding agent proposes a bounded initial set:

$$
\mathcal{A}(n)=\{a_1,\ldots,a_K\}
$$

where every $a_i$ is a structured improvement hypothesis $(F,C,M,O)$.

For each candidate, record:

- supporting evidence,
- contradictory evidence,
- expected transfer/generalization,
- estimated implementation difficulty,
- predicted behavioral effect,
- an initial search prior.

The agent supplies a prior-like score, but this score is only **initial guidance**. Search outcomes can override it.

## Three quantities that must remain separate

### 1. Epistemic belief

$$
B(C_i)\in[0,1]
$$

How strongly does current evidence support causal claim $C_i$?

Causal claims are **not assumed mutually exclusive**. Grounding may be unreliable *and* escalation may be missing, so beliefs should not be forced to sum to one.

### 2. Search prior

$$
\pi(a_i\mid n)
$$

How much initial search budget should action $a_i$ receive?

This can depend on belief, expected impact, novelty, cost, and transfer potential:

$$
\pi(a_i\mid n)
=
\operatorname{softmax}
\left(
f(B(C_i),\text{impact},\text{cost},\text{novelty},\ldots)
\right)
$$

The priors are normalized for search allocation even though causal beliefs need not be.

### 3. Empirical branch value

$$
Q(n,a)
=
\mathbb{E}[\text{future improvement utility}\mid n,a]
$$

How productive has pursuing this improvement direction actually been?

Therefore:

$$
\boxed{\text{belief }B \neq \text{search prior }\pi \neq \text{empirical value }Q}
$$

## Tree policy

Use a PUCT-style selection rule:

$$
a^*
=
\arg\max_a
\left[
Q(n,a)
+
c\,\pi(a\mid n)
\frac{\sqrt{N(n)}}{1+N(n,a)}
\right]
$$

where:

- $Q(n,a)$: empirical value of action $a$,
- $\pi(a\mid n)$: initial search prior,
- $N(n)$: visits / allocated experiments at node $n$,
- $N(n,a)$: experiments allocated through action $a$,
- $c$: exploration coefficient.

This gives the desired behavior:

- promising hypotheses receive early budget,
- underexplored alternatives remain reachable,
- empirical downstream success can override the coding agent's initial preference.

## Progressive widening

The action space is generative: the coding agent can keep inventing new improvement hypotheses. Unlike Go, there is no fixed finite legal-action set.

To avoid creating a new action on every visit, use progressive widening:

$$
|\mathcal{A}(n)| < kN(n)^\alpha, \qquad 0<\alpha<1
$$

If the inequality holds, the controller may ask the coding agent for another candidate improvement hypothesis. Otherwise, it must select among existing actions.

Intuition:

> **More evidence/budget at a node permits more alternative improvement directions, but branch width grows more slowly than the number of visits.**
> 

For the first prototype, this can be simplified to a fixed initial set, e.g. 2–3 improvement hypotheses, and generate another only when existing hypotheses have all been tried and the node remains promising.

## Stochastic transition: hypothesis → code

In AlphaGo, executing a move has a known transition:

$$
s_{t+1}=T(s_t,a_t)
$$

For AlphaAgents, the coding agent is part of the transition:

$$
H_{t+1}\sim T_\theta(H_t,a_t)
$$

The same improvement hypothesis can produce different code patches.

Therefore every expansion must store the **exact generated artifact**:

$$
n_t
\xrightarrow{(a_t,\Delta H_t)}
n_{t+1}
$$

Selecting an existing child again means evaluating or expanding that exact harness. Asking the coding agent to implement the same hypothesis again creates a **new child sample**, not the same transition.

## Expansion

When action $a=(F,C,M,O)$ is selected:

1. Give the coding agent the parent harness, relevant evidence, and the selected improvement hypothesis.
2. Ask it to implement modification $M$ as a focused patch.
3. Materialize an exact child harness.
4. Check **reasoning–implementation alignment**:
    - Does the diff actually implement $M$?
    - Did the intended code path execute?
    - Were unrelated edits introduced?
    - Is the predicted mechanism $O$ observable/testable?
5. Run the screen evaluation.

This lets us distinguish three failure modes:

- **Hypothesis error:** causal explanation $C$ was wrong.
- **Intervention/design error:** proposed modification $M$ was insufficient or inappropriate.
- **Implementation error:** the generated code did not faithfully realize $M$.

## Evaluation protocol

### 1. Screen

Cheap, search-visible checks:

- runtime / syntax validity,
- targeted diagnostic probe,
- target task,
- minimal regression checks,
- whether the modified path actually executed.

Failing candidates can be rejected cheaply.

### 2. Selection

Broader development evaluation used by tree search:

$$
U_{\text{selection}}(H)
$$

Selection results can update $Q$, retention decisions, and search allocation.

### 3. Confirmation

Protected held-out tasks / fresh runs:

$$
U_{\text{confirm}}(H)
$$

Confirmation is used for promoted candidates and final claims. It should **not be repeatedly backpropagated into ordinary search**, otherwise the held-out set becomes another optimization set.

## Reward and backup

Store an absolute harness utility:

$$
U(n)
$$

based on task performance, regression, validity, and possibly cost.

Use **parent-relative improvement** as the immediate reward:

$$
r_t=
U(n_{t+1})-U(n_t)-\lambda_c C_t
$$

where $C_t$ is generation/evaluation cost.

For a path

$$
n_0\xrightarrow{a_0}n_1
\xrightarrow{a_1}\cdots
\xrightarrow{a_{L-1}}n_L
$$

define downstream return:

$$
G_t=
r_t+\gamma r_{t+1}+\gamma^2r_{t+2}+\cdots
$$

Then use ordinary empirical MCTS backup:

$$
N(n_t,a_t)\leftarrow N(n_t,a_t)+1
$$

$$
Q(n_t,a_t)
\leftarrow
Q(n_t,a_t)
+
\frac{G_t-Q(n_t,a_t)}{N(n_t,a_t)}
$$

This propagates downstream utility only along the path actually explored.

The absolute utility $U(n)$ is still retained for comparing and selecting final harnesses.

## Belief update is separate from value backup

Search-value backup should **not** be interpreted as proving a causal hypothesis true.

Evaluation can produce four qualitatively different outcomes:

| Predicted mechanism changed? | Benchmark improved? | Interpretation |
| --- | --- | --- |
| Yes | Yes | Mechanism supported and intervention useful |
| Yes | No | Mechanism may be correct, but intervention has poor utility / causes regressions |
| No | Yes | Performance improved, but likely not for the claimed reason |
| No | No | Hypothesis, implementation, or both are unsupported |

Update $B(C)$ from diagnostic/mechanistic evidence. Update $Q(n,a)$ from empirical downstream utility. Keep these updates separate.

## Example

Observed failure:

> The mobile agent repeatedly executes the same action pattern without making progress.
> 

Candidate improvement hypotheses:

- $a_1$: **Improve grounding verification** because repeated taps may be caused by incorrect target coordinates; expect target confirmation accuracy to improve.
- $a_2$: **Add failure escalation and replanning** because repetition is detected but does not alter control flow; expect loops to terminate and a new plan to be produced.
- $a_3$: **Add UI-state-change detection** because the harness cannot recognize that an action had no effect; expect unchanged states to trigger recovery.
- $a_4$: **Generalize the repetition signature** because recovery exists but the detector misses semantically equivalent action cycles; expect more loops to be recognized.

Possible tree:

```
H0
├── a1: grounding verification
│   └── H1
├── a2: failure escalation + replan
│   └── H2
│       ├── a21: pass failure context to planner → H4
│       └── a22: adjust escalation condition → H5
├── a3: UI-state-change detection
│   └── H3
└── a4: generalize repetition detector
    └── H6
```

Suppose $a_2$ has only moderate prior, but H2 and its descendants repeatedly improve several tasks with limited regression. Then $Q(H_0,a_2)$ rises and the controller allocates more budget to that branch.

This is analogous to AlphaGo allowing empirical search evidence to override the policy network's initial preference.

## Relationship to the two AlphaAgents search levels

### RQ1 — Global lineage search

Question:

> **Which harness / archive region should receive the next improvement budget?**
> 

The global controller tracks statistics such as:

$$
Q_G(H),\;N_G(H)
$$

and allocates a local search budget $b_H$ to a selected parent.

### RQ2 — Local improvement-hypothesis search

Question:

> **Given the selected parent, what should the coding agent try to change next?**
> 

The local controller tracks:

$$
B(C),\;\pi(a),\;Q_L(n,a),\;N_L(n,a)
$$

The two levels should initially have **separate statistics**, because their visits and values refer to different decisions.

Conceptually:

$$
\text{Global parent selection}
\rightarrow
\boxed{\text{local improvement-hypothesis search}}
\rightarrow
\text{new child harnesses}
\rightarrow
\text{global archive update}
$$

The global controller allocates budget to a parent; the local controller decides how that budget is spent across improvement hypotheses.

## Tree traversal: revisit existing children vs. expand a new child

One local MCTS simulation should perform **selection repeatedly down the existing tree**, expand at most one new child, evaluate that new leaf, and then backpropagate through the full selected path.

At node $n$:

1. Ensure candidate improvement hypotheses $\mathcal{A}(n)$ exist. If progressive widening permits, the controller may add a new candidate action.
2. Use PUCT to choose an action $a^*$.
3. If $a^*$ has **not yet been implemented**, stop tree traversal and **expand** it by generating one concrete child harness.
4. If $a^*$ already has an exact child harness, **revisit and descend into that child** rather than generating another patch.
5. Repeat selection from that child until an unexpanded action is chosen.

For the first implementation, use **one concrete child per improvement hypothesis**. This makes the transition rule simple:

$$
\text{selected action unexpanded} \Rightarrow \text{implement and create child}
$$

$$
\text{selected action already expanded} \Rightarrow \text{descend into existing child}
$$

Later, if we want multiple stochastic implementations of the same improvement hypothesis, add a second progressive-widening rule over concrete implementations. Those regenerated implementations should be treated as distinct children.

Example:

```
H0
├── a1 → H1
├── a2 → H2
│         ├── a21 → H4
│         └── a22 → H5
└── a3 → H3
```

Suppose PUCT selects $a_2$ at $H_0$. Since $a_2$ has already been expanded, the simulation descends to $H_2$. At $H_2$, PUCT selects $a_{22}$. If $a_{22}$ is also already expanded, descend to $H_5$ and continue. If instead PUCT selects an unexpanded $a_{23}$, the coding agent implements $a_{23}$ once, creating a new leaf $H_6$.

Thus a simulation path can be:

$$
H_0
\xrightarrow{a_2}
H_2
\xrightarrow{a_{22}}
H_5
\xrightarrow{a_{53}}
H_6
$$

where only the final edge $a_{53}$ is newly expanded in that simulation.

After evaluating $H_6$, backpropagate its downstream return along **all selected edges**:

$$
(H_5,a_{53}),\;(H_2,a_{22}),\;(H_0,a_2)
$$

This is what allows an early improvement hypothesis to gain value from useful descendants several generations later.

## One full local MCTS simulation

```jsx
INPUT:
    local root n0 = selected parent harness
    local search tree T
    remaining local budget b

path ← []
n ← n0

1. SELECTION / DESCENT LOOP

   while True:

       failures ← GetRelevantFailures(n)
       evidence ← LoadEvidence(n)
       actions ← LoadExistingImprovementHypotheses(n)

       if actions are insufficient
          or ProgressiveWideningAllowsNewAction(n):

           new_actions ← Agent.GenerateImprovementHypotheses(
               n.code,
               failures,
               evidence,
               prior experiments at n
           )

           store each action as:
               target failure
               causal claim
               proposed modification
               predicted outcome
               evidence for/against
               belief score

           derive / update search priors π(a|n)

       a* ← argmax_a [
           Q(n,a)
           + c π(a|n) sqrt(N(n)) / (1 + N(n,a))
       ]

       path.append((n, a*))

       if a* has NO concrete child yet:
           break                       # expand this action

       n ← ExistingChild(n, a*)       # revisit and descend

2. EXPANSION

   patch ← CodingAgent.Implement(n, a*)
   child ← MaterializeExactHarness(patch)

   attach child to edge (n, a*) in T

3. ALIGNMENT CHECK

   verify:
       diff implements selected modification
       intended path executes
       unrelated edits are recorded
       predicted mechanism can be tested

   if invalid:
       leaf_return ← InvalidExpansionPenalty
       Backpropagate(path, leaf_return)
       stop simulation

4. SCREEN

   screen_result ← RunScreen(child)

   if clearly failed:
       update local evidence / causal belief
       leaf_return ← ScreenFailureReturn
       Backpropagate(path, leaf_return)
       stop simulation

5. SELECTION EVALUATION

   result ← EvaluateSelectionSet(child)
   U_child ← Utility(result)

6. BELIEF UPDATE

   update B(C) for the selected action using
   mechanistic / diagnostic observations

   recompute π where relevant

7. LEAF RETURN

   For each ancestor node nt on path, define:

       G_t ← U(child) - U(nt) - λ_cost * C_sim

   where C_sim is the NEW cost incurred by this simulation
   (generation + screen + evaluation of the new leaf).

   Do not charge historical generation costs again merely
   because an existing child was revisited.

8. BACKUP

   for each (nt, at) in path:

       N(nt)    ← N(nt) + 1
       N(nt,at) ← N(nt,at) + 1

       Q(nt,at) ← Q(nt,at)
                  + (G_t - Q(nt,at)) / N(nt,at)

9. ARCHIVE UPDATE

   if child passes retention criteria:
       add exact child to local/global archive

10. OPTIONAL CONFIRMATION

    if child satisfies promotion criteria:
        evaluate on protected confirmation set
        record result
        do not routinely feed confirmation back into search
```

A **local search episode** consists of repeating these simulations until the budget $b$ is exhausted. The global controller then receives the local-search outcome and updates its parent-selection statistics.

## Difference from current DGM-style mutation

Current style:

```
select parent
→ ask coding agent for a mutation
→ evaluate child
```

Proposed style:

```
select parent
→ expose accumulated failure evidence
→ generate explicit improvement hypotheses
→ assign search priors
→ PUCT selects what the coding agent should try
→ coding agent implements that hypothesis
→ verify implementation alignment
→ evaluate exact child
→ update causal beliefs and empirical branch value separately
→ backpropagate downstream evidence
→ choose next expansion
```

The proposal therefore turns child generation from **sampling patches** into **persistent, evidence-driven deliberation over possible improvement actions**.

## Optional TD extension

The initial implementation should use empirical MCTS backup.

Only call the method TD-style if values are bootstrapped from successor estimates, e.g.:

$$
\delta_t=r_t+\gamma V(n_{t+1})-V(n_t)
$$

$$
V(n_t)\leftarrow V(n_t)+\alpha\delta_t
$$

A learned $V(n)$ could eventually estimate the future potential of partially explored harness states, but it should be deferred until there is evidence that such a value estimator is reliable.

## Experimental design

Compare under matched generation/evaluation budgets:

| Method | Description |
| --- | --- |
| Direct mutation baseline | One unconstrained coding-agent mutation per selected parent |
| Improvement-hypothesis sampling | Generate $K$ structured actions and evaluate each once; no search/backprop |
| Improvement-hypothesis MCTS | LLM priors + PUCT allocation + empirical backup |
| MCTS + learned value / TD | Add bootstrapped value estimation for partially explored states |

Primary measurements:

- confirmed improvement per valid evaluation,
- cost/time to first confirmed improvement,
- regression rate,
- repeated / redundant improvement directions,
- task transfer across failures/apps,
- prior-vs-outcome calibration,
- hypothesis-to-diff alignment,
- predicted-mechanism vs observed-mechanism agreement,
- fraction of search budget spent on productive branches.

## Important complications

- **Generative action space:** the coding agent can invent arbitrarily many improvement hypotheses; progressive widening controls this.
- **Stochastic implementation:** the same hypothesis can yield different patches; store exact artifacts and treat regenerated implementations as distinct children.
- **Mechanistic support vs utility:** a predicted mechanism can be correct even when task performance does not improve.
- **Patch confounding:** bundled edits weaken causal attribution; prefer focused changes when testing a hypothesis.
- **Noisy evaluation:** matched tasks/seeds and confirmation runs are needed before strong value updates or final claims.
- **Transpositions:** semantically equivalent improvement hypotheses or near-equivalent patches may appear under different wording or parents.
- **Adaptive overfitting:** repeatedly using the same development tasks can overfit search; confirmation must remain protected.
- **Two-level credit assignment:** local hypothesis-search outcomes and global parent-selection outcomes should be recorded separately.

## Working hypothesis

> **Explicitly representing candidate improvement actions and using tree search to allocate coding experiments will improve sample efficiency, credit assignment, and transfer compared with directly sampling unconstrained code mutations.**
> 

The most conservative first implementation is **PUCT-guided improvement-hypothesis search with empirical MCTS backup**, using structured actions $(F,C,M,O)$ and a protected screen/selection/confirmation evaluation protocol.