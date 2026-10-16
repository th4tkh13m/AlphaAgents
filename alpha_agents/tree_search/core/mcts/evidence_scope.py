"""Resolve explicitly declared value evidence without granting transitive reads."""

import hashlib
import json
from pathlib import Path


def archived_task_roots(request, *, files=False):
    """Resolve only task roots or exact files declared by archived plan attempts.

    These can include diagnostics: allowing a citation does not promote a
    diagnostic into a canonical benchmark sample. Judges retain their separate
    canonical-only contract, and values use their sealed continuation bundle.
    """
    if request.role not in {"proposer", "policy", "implementer"}:
        return []
    roots = []
    run = Path(request.run_id).resolve()
    for plan in request.context.get("plan_archive", {}).get("plans", []):
        for attempt in plan.get("attempts", []):
            references = attempt.get("canonical_evidence", [])
            if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
                raise ValueError("Archived task roots must be a list of strings")
            for reference in references:
                path = Path(reference)
                if not path.is_absolute():
                    continue  # Symbolic adapter references are not read grants.
                if not path.is_relative_to(run):
                    raise ValueError("Archived task root must belong to this run")
                parts = path.relative_to(run).parts
                if (len(parts) < 4 or parts[0] != "tasks" or parts[3] != "evidence"
                        or ".." in path.parts or path.resolve() != path
                        or not (path.is_dir() if len(parts) == 4 else path.is_file())):
                    raise ValueError("Archived task reference must identify an existing canonical task evidence root or file")
                if (path.is_file() and files) or (path.is_dir() and not files):
                    roots.append(path)
    return sorted(set(roots))


def continuation_prior_ids(bundle):
    """Typed prior identifiers from the hash-verified continuation snapshot."""
    if bundle is None:
        return set()
    identifiers = {bundle.get("prior_version")}
    identifiers.update(node.get("prior_version") for node in bundle.get("reachable_subtree", []))
    identifiers.update(evidence.get("prior", {}).get("id")
                       for evidence in bundle["reachable_evidence"].values()
                       if evidence.get("prior") is not None)
    return {identifier for identifier in identifiers if isinstance(identifier, str) and identifier}


def continuation_bundle(request):
    if request.role != "value":
        return None
    declaration = request.context.get("continuation_evidence")
    if not declaration or "evidence_bundle_path" not in declaration:
        return None
    path = Path(declaration["evidence_bundle_path"])
    run = Path(request.run_id).resolve()
    if (not path.is_absolute() or not path.is_relative_to(run) or path.resolve() != path
            or len(path.relative_to(run).parts) != 3 or path.relative_to(run).parts[0] != "evidence_archive"
            or path.name != "CONTINUATION.json" or not path.is_file()):
        raise ValueError("Continuation bundle must be an exact immutable file in this run")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != declaration.get("evidence_bundle_hash"):
        raise ValueError("Continuation bundle hash mismatch")
    payload = json.loads(content)
    expected = request.context.get("capacity_snapshot_id", declaration.get("capacity_snapshot_id"))
    if (not isinstance(payload, dict) or not expected or payload.get("snapshot_id") != expected
            or declaration.get("capacity_snapshot_id") != expected
            or not isinstance(payload.get("reachable_evidence"), dict)):
        raise ValueError("Continuation bundle does not identify this frozen frontier")
    return payload


def value_task_roots(request, *, files=False):
    if request.role != "value":
        return []
    declaration = request.context.get("continuation_evidence", {})
    references = declaration.get("canonical_evidence", [])
    if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
        raise ValueError("Continuation task roots must be a list of strings")
    run = Path(request.run_id).resolve()
    roots = []
    for reference in references:
        path = Path(reference)
        if not path.is_absolute():
            continue  # Existing symbolic test/adapter references are not read grants.
        if not path.is_relative_to(run):
            raise ValueError("Continuation task root must belong to this run")
        parts = path.relative_to(run).parts
        if (len(parts) < 4 or parts[0] != "tasks" or parts[3] != "evidence"
                or path.resolve() != path or not (path.is_dir() if len(parts) == 4 else path.is_file())):
            raise ValueError("Continuation task root must be an existing canonical task evidence directory")
        if (path.is_file() and files) or (path.is_dir() and not files):
            roots.append(path)
    return sorted(set(roots))
