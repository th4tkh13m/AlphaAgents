"""Optional regression checks for Python tests changed by a mutation."""

import sys
from pathlib import Path

from ..core.contracts import ValidationReport
from ..core.workspace import git
from ..infrastructure.process import execute


class ChangedPytestValidator:
    def __init__(self, python: str = sys.executable, timeout: float = 300):
        if timeout <= 0:
            raise ValueError("Validation timeout must be positive")
        self.python, self.timeout = python, timeout

    def identity(self):
        return {
            "type": "changed_pytest",
            "python": self.python,
            "timeout": self.timeout,
        }

    def validate(self, candidate, context):
        changed = git(
            candidate.workspace, "diff", "--name-only", candidate.base_commit
        ).splitlines()
        tests = [
            name
            for name in changed
            if (candidate.workspace / name).is_file()
            and (
                Path(name).name.startswith("test_")
                and name.endswith(".py")
                or name.endswith("_test.py")
            )
        ]
        if not tests:
            return ValidationReport("changed_pytest", "skipped", {"tests": []})
        artifacts = candidate.directory / "validation" / "changed_pytest"
        result, _ = execute(
            [self.python, "-m", "pytest", "-q", *tests],
            candidate.workspace,
            artifacts,
            self.timeout,
        )
        failed = result["timed_out"] or result["returncode"] != 0
        return ValidationReport(
            "changed_pytest",
            "failed" if failed else "completed",
            {"tests": tests, **result},
            (str(artifacts),),
            "Regression tests failed or timed out" if failed else None,
        )
