"""Deterministic demo mutation, replacing an LLM for offline validation."""

import sys
from pathlib import Path

(Path(sys.argv[1]) / "agent.py").write_text("def solve(value):\n    return value + 1\n")
