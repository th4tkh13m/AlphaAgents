"""Provider-neutral role request interface consumed by the MCTS controller."""
from typing import Any, Protocol


class RoleRequest(Protocol):
    run_id: str
    request_id: str
    role: str
    artifact_id: str
    model: str
    effort: str
    context: dict[str, Any]
    timeout_seconds: int
    prompt_version: str

    @property
    def library_hash(self) -> str: ...
    @property
    def prompt_hash(self) -> str: ...
    @property
    def permission_mode(self) -> str: ...
    @property
    def collaboration_mode(self) -> str: ...
    @property
    def execution_mode(self) -> str: ...
    def render(self) -> str: ...
    def to_record(self) -> dict[str, Any]:
        """Serializable request fields, independent of the adapter's object type."""
        ...
