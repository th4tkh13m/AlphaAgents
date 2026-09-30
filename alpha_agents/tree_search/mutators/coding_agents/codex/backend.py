"""Codex SDK mutation backend; independent of every harness bridge."""

import os
import sys
from pathlib import Path

from ....core.contracts import MutationOutcome
from ....infrastructure.process import execute


class CodexMutator:
    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        timeout: float | None = None,
        prompt_template: Path | None = None,
    ):
        self.model, self.effort = model, effort
        self.timeout = timeout if timeout and timeout > 0 else None
        self.prompt_template = prompt_template.resolve() if prompt_template else None

    def identity(self):
        return {
            "type": "codex",
            "model": self.model,
            "effort": self.effort,
            "timeout": self.timeout,
            "prompt_template": str(self.prompt_template)
            if self.prompt_template
            else None,
        }

    def mutate(self, candidate, context):
        env = os.environ.copy()
        if self.model:
            env["DGM_CODEX_MODEL"] = self.model
        if self.effort:
            env["DGM_CODEX_EFFORT"] = self.effort
        if self.prompt_template:
            env["DGM_PROMPT_TEMPLATE"] = str(self.prompt_template)
        command = [
            sys.executable,
            str(Path(__file__).with_name("worker.py")),
            "--context",
            str(candidate.mutation_context),
            "--git_dir",
            str(candidate.workspace),
            "--chat_history_file",
            str(candidate.directory / "self_evo.md"),
            "--outdir",
            str(candidate.directory),
        ]
        result, _ = execute(
            command,
            candidate.workspace,
            candidate.directory / "mutation",
            self.timeout,
            env,
        )
        if result["timed_out"] or result["returncode"]:
            return MutationOutcome(
                "failed",
                "Codex timed out" if result["timed_out"] else "Codex worker failed",
            )
        return MutationOutcome("completed")
