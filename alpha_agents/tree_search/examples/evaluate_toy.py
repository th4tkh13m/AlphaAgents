"""Trusted evaluator lives outside the editable toy harness."""

import importlib.util
import json
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("candidate_agent", workspace / "agent.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cases = [(1, 2), (2, 3), (5, 6), (-1, 0)]
passed = sum(module.solve(value) == expected for value, expected in cases)
print(
    json.dumps(
        {
            "status": "completed",
            "score": passed / len(cases),
            "metrics": {"passed": passed, "total": len(cases)},
        }
    )
)
