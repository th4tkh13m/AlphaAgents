"""Use an external code-editing process as the mutation backend."""

from ..core.contracts import MutationOutcome
from ..infrastructure.commands import expand_command
from ..infrastructure.process import execute


class CommandMutator:
    def __init__(self, command: list[str], timeout: float = 300):
        if not command or timeout <= 0:
            raise ValueError("Mutation command and positive timeout are required")
        self.command, self.timeout = list(command), timeout

    def identity(self):
        return {"type": "command", "command": self.command, "timeout": self.timeout}

    def mutate(self, candidate, context):
        context_path = candidate.mutation_context
        values = {
            "workspace": str(candidate.workspace),
            "artifacts": str(candidate.directory),
            "context": str(context_path),
        }
        command = expand_command(self.command, values)
        result, _ = execute(
            command, candidate.workspace, candidate.directory / "mutation", self.timeout
        )
        if result["timed_out"] or result["returncode"]:
            return MutationOutcome(
                "failed",
                "Mutation timed out"
                if result["timed_out"]
                else "Mutation command failed",
            )
        return MutationOutcome("completed")
