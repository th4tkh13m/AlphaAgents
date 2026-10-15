# DGM archive search

This package owns the Darwin Gödel Machine search path. It selects a parent
from an archive, edits a disposable candidate, independently evaluates the
patch, and checkpoints the candidate lineage and parent pools.

| File | Responsibility |
| --- | --- |
| `contracts.py` | Candidate, mutation/evaluation results, bridge, mutator, and validator contracts |
| `controller.py` | `DGMController`, `SearchConfig`, scheduling, admission, and resume |
| `workspace.py` | Reconstruct candidate Git workspaces and capture/recover canonical patches |
| `storage.py` | Candidate records and authoritative `state.json` checkpoints |

Start with `contracts.py`, then follow `DGMController.run()` and `_child()`.
See the [algorithm guide](../../DGM_ALGORITHM.md) for the complete lifecycle.

The public imports remain available:

```python
from alpha_agents.tree_search import DGMController, SearchConfig
```

Internal imports now use `alpha_agents.tree_search.core.dgm`. There are no
implementations or compatibility modules left at the old `core/controller.py`,
`core/contracts.py`, `core/storage.py`, or `core/workspace.py` locations.

Both DGM and MCTS use `core/shared/storage.py` for atomic JSON and the same
exclusive run lock, and `core/shared/workspace.py` for Git commands. Shared
utilities do not import either algorithm. MCTS does not import DGM's candidate
records or workspace reconstruction.

This is a code-organization change. DGM configuration, scores, patches, and
JSON run formats retain their existing behavior. Existing runs can resume
with the same configuration and output directory. Production evaluator source
hashes still detect changes to adapter imports, so frozen MCTS runtime identities
must continue to match their original evaluation software.
