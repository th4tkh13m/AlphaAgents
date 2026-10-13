"""Hash-verified, recoverable file exports of authoritative SQLite plan records."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from alpha_agents.tree_search.core.mcts.contracts import canonical_json, fingerprint


def document_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def freeze_document(directory: Path, plan_id: str, document: str,
                    expected_hash: str | None = None) -> dict[str, str]:
    """Publish once without overwriting; missing exports are rebuilt from SQLite.

    A corrupt existing export is an integrity error, never silently repaired.
    Hash-derived directories prevent plan IDs from escaping the archive root.
    Concurrent exporters link their complete temporary file atomically.
    """
    digest = document_hash(document)
    if expected_hash is not None and expected_hash != digest:
        raise ValueError("Authoritative plan document hash mismatch")
    path = _freeze_bytes(directory, plan_id, "PLAN.md", document.encode("utf-8"))
    return {"document_path": path, "document_hash": digest}


def freeze_metadata(directory: Path, plan_id: str, metadata: dict) -> dict[str, str]:
    content = canonical_json(metadata).encode("utf-8")
    path = _freeze_bytes(directory, plan_id, "PLAN.json", content)
    return {"metadata_path": path, "metadata_hash": hashlib.sha256(content).hexdigest()}


def freeze_attempt_evidence(directory: Path, payload: dict) -> dict[str, str]:
    """A versioned readable snapshot; later outcomes never overwrite older input."""
    content = canonical_json(payload).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = _freeze_bytes(directory, digest, "ATTEMPT.json", content, archive="evidence_archive")
    return {"evidence_bundle_path": path, "evidence_bundle_hash": digest}


def freeze_continuation_evidence(directory: Path, payload: dict) -> dict[str, str]:
    content = canonical_json(payload).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = _freeze_bytes(directory, digest, "CONTINUATION.json", content, archive="evidence_archive")
    return {"evidence_bundle_path": path, "evidence_bundle_hash": digest}


def _freeze_bytes(directory: Path, plan_id: str, name: str, content: bytes,
                  *, archive: str = "plan_archive") -> str:
    root = directory / archive
    parent = root / fingerprint({"plan_id": plan_id})
    if root.is_symlink() or parent.is_symlink():
        raise ValueError("Frozen archive export cannot use a symbolic link")
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".plan-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
    if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
        raise ValueError("Frozen plan document export is corrupt")
    return str(path.resolve())
