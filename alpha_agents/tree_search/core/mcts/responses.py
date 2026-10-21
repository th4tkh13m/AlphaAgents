"""Provider-independent role evidence and semantic response validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from alpha_agents.tree_search.core.mcts.contracts import judge_aggregate
from alpha_agents.tree_search.core.mcts.evidence_scope import (
    archived_task_roots,
    continuation_bundle,
    continuation_prior_ids,
    value_task_roots,
)
from alpha_agents.tree_search.core.mcts.planning_scope import (
    declares_same_parent_duplicate,
)
from alpha_agents.tree_search.core.mcts.policy import prepare_priors
from alpha_agents.tree_search.core.mcts.role_contracts import (
    RoleRequest,
)
from alpha_agents.tree_search.core.mcts.storage import normalize_intervention


def _refs(context: Any) -> set[str]:
    result = set()
    if isinstance(context, dict):
        for key, value in context.items():
            if key == "prior_implementations":
                continue  # Curated report prose is not a transitive citation grant.
            if (key.endswith("_id") or key.endswith("_snapshot_id")) and isinstance(value, str):
                result.add(value)
            if key.endswith("_path") and isinstance(value, str):
                result.add(value)
            if (key == "evidence" or key.endswith("_ids")) and isinstance(value, list):
                result.update(ref for ref in value if isinstance(ref, str))
            result.update(_refs(value))
    elif isinstance(context, list):
        for value in context:
            result.update(_refs(value))
    return result


def file_reference_catalog(request: RoleRequest) -> dict[str, dict]:
    """Bind unique supplied identifiers to sealed canonical task files."""
    catalog = {}
    canonical = request.context.get("canonical_evidence", {})
    for bundle in canonical.get("file_catalog", []):
        for item in bundle["files"]:
            identifier = item.get("reference_id")
            if identifier is None:
                continue  # Older requests retain their direct-path contract.
            if not isinstance(identifier, str) or not re.fullmatch(r"s0-file-[0-9]{4,}", identifier) or identifier in catalog:
                raise ValueError("Invalid or duplicate canonical file reference ID")
            catalog[identifier] = {**item, "evidence_root_path": bundle["evidence_root_path"]}
    return catalog


def verified_catalog_file(item: dict, request: RoleRequest) -> str:
    canonical = request.context["canonical_evidence"]
    root = Path(item["evidence_root_path"])
    path = Path(item["absolute_file"])
    relative = Path(item["path"])
    if (str(root) not in canonical.get("evidence", []) or not root.is_absolute()
            or root.resolve() != root or relative.is_absolute() or ".." in relative.parts
            or path != root / relative or path.resolve() != path or not path.is_file()
            or not path.is_relative_to(root)):
        raise ValueError("Canonical file reference escapes its declared task root")
    data = path.read_bytes()
    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError("Canonical file reference no longer matches sealed content")
    return str(path)


def canonical_file_evidence(result: dict, request: RoleRequest) -> tuple[dict, list[dict]]:
    """Resolve validated catalog IDs; retain the original response separately."""
    catalog = file_reference_catalog(request) if request.role.startswith("judge_") else {}
    mappings = {}
    def reference(ref):
        if isinstance(ref, str) and ref in catalog:
            if ref in mappings:
                return mappings[ref]["canonical"]
            canonical = verified_catalog_file(catalog[ref], request)
            mappings[ref] = {"original": ref, "canonical": canonical,
                             "size": catalog[ref]["size"], "sha256": catalog[ref]["sha256"]}
            return canonical
        return ref
    def visit(value):
        if isinstance(value, dict):
            return {key: visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        if isinstance(value, str):
            # Judge prose is forwarded to later roles without this invocation's
            # file-ID catalog. Keep its citations usable, with the same sealed
            # file validation and provenance as evidence-array references.
            return re.sub(r"(?<![\w/-])s0-file-[0-9]{4,}(?![\w/-])",
                          lambda match: reference(match.group()), value)
        return value
    return visit(result), [mappings[key] for key in sorted(mappings)]


def _evidence(refs: Any, request: RoleRequest, workspace: Path):
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("Role findings require nonempty exact evidence references")
    known = _refs(request.context.get("canonical_evidence", {})) if request.role.startswith("judge_") else _refs(request.context)
    if not request.role.startswith("judge_"):
        history = request.context.get("prior_implementations", {})
        typed_ids = [history.get("snapshot_id")]
        for entry in history.get("implementations", []):
            typed_ids.extend(entry.get(key) for key in ("report_id", "attempt_id", "node_id", "artifact_id", "plan_id"))
            outcome = entry.get("canonical_outcome", {})
            typed_ids.extend(outcome.get(key) for key in ("u_snapshot_id", "j_snapshot_id"))
            # These identify supplied summary observations, not permission to
            # open their underlying evidence or model-authored report paths.
            for check in entry.get("implementation", {}).get("check_requests", []):
                typed_ids.extend(check.get(key) for key in ("snapshot_id", "artifact_id"))
        known.update(ref for ref in typed_ids if isinstance(ref, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", ref))
    roots = value_task_roots(request) + archived_task_roots(request)
    known.update(str(path) for path in value_task_roots(request, files=True))
    known.update(str(path) for path in archived_task_roots(request, files=True))
    bundle = continuation_bundle(request)
    if bundle is not None:
        known.update(_refs(bundle["reachable_evidence"]))
        known.update(continuation_prior_ids(bundle))
    if not request.role.startswith("judge_"):
        known.update(f"context:{key}" for key, value in request.context.items()
                     if key.endswith("_id") and isinstance(value, str) and value)
    known.update(_diagnostic_snapshot_refs(request))
    root = workspace.resolve()
    catalog = file_reference_catalog(request) if request.role.startswith("judge_") else {}
    for ref in refs:
        if request.role == "value" and Path(ref).is_absolute() and Path(ref).is_dir():
            raise ValueError("Value evidence citations must identify files rather than directories")
        if ref in catalog:
            verified_catalog_file(catalog[ref], request)
            continue
        if ref.startswith("s0-file-"):
            raise ValueError(f"Unknown canonical file reference: {ref}")
        if ref in known:
            continue
        path_text = re.sub(r":\d+(?::\d+)?$", "", ref.removeprefix("code:"))
        if path_text in known and Path(path_text).is_file():
            continue  # A declared immutable archive file, optionally with a location.
        try:
            path = (root / path_text).resolve()
            existing_file = path.is_file()
        except (OSError, ValueError) as error:
            raise ValueError(f"Unknown or out-of-scope evidence reference: {ref}") from error
        if request.role.startswith("judge_") and existing_file and any(
            Path(reference).is_absolute() and Path(reference).is_dir()
            and path.is_relative_to(Path(reference).resolve())
            for reference in request.context.get("canonical_evidence", {}).get("evidence", [])
        ):
            continue  # Files beneath the judge's explicitly supplied task roots.
        if not request.role.startswith("judge_") and existing_file:
            if any(path.is_relative_to(directory) for directory in roots):
                raw = Path(path_text)
                if ".." in raw.parts or raw.resolve() != raw:
                    raise ValueError("Evidence path escapes its declared canonical location")
                continue
            run = Path(request.run_id).resolve()
            for reference in known:
                evidence_root = Path(reference)
                if not evidence_root.is_absolute() or not evidence_root.is_relative_to(run):
                    continue
                relative = evidence_root.relative_to(run).parts
                if (len(relative) == 4 and relative[0] == "tasks" and relative[3] == "evidence"
                        and evidence_root.is_dir() and evidence_root.resolve() == evidence_root
                        and path.is_relative_to(evidence_root)):
                    break
            else:
                evidence_root = None
            if evidence_root is not None:
                continue  # Same canonical task-directory reads as the permission builder.
        channel = request.context.get("device_check_channel")
        if request.role == "implementer" and channel and existing_file and path.is_relative_to(Path(channel["result_directory"]).resolve()):
            continue
        if request.role == "value" and existing_file and any(
            path.is_relative_to(Path(view["code_path"]).resolve())
            for view in request.context.get("reachable_code_views", [])
        ):
            continue
        if not path.is_relative_to(root) or not existing_file:
            raise ValueError(f"Unknown or out-of-scope evidence reference: {ref}")


def _diagnostic_snapshot_refs(request: RoleRequest) -> set[str]:
    """Recognize completed controller receipts owned by this implementer call."""
    channel = request.context.get("device_check_channel")
    if request.role != "implementer" or not channel:
        return set()
    root = Path(channel["result_directory"])
    expected = Path(request.run_id) / "role_requests" / request.request_id / "check_results"
    if root != expected or root.resolve() != expected.absolute() or not root.is_dir():
        return set()
    snapshots = set()
    for path in root.glob("*.json"):
        if path.is_symlink() or not re.fullmatch(r"(?:latest|[0-9a-f]{64})\.json", path.name):
            continue
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict):
            continue
        snapshot = receipt.get("snapshot_id")
        if (receipt.get("status") == "completed" and receipt.get("sample_kind") == "targeted"
                and receipt.get("invocation_id") == request.request_id
                and receipt.get("attempt_id") == request.context.get("attempt_id")
                and isinstance(receipt.get("check_id"), str) and receipt["check_id"]
                and isinstance(snapshot, str) and re.fullmatch(r"[0-9a-f]{64}", snapshot)
                and isinstance(receipt.get("artifact_id"), str)
                and re.fullmatch(r"[0-9a-f]{64}", receipt["artifact_id"])):
            snapshots.add(snapshot)
    return snapshots


def canonical_code_evidence(result: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Rebase validated local code citations without widening filesystem access.

    Only evidence-array entries naming files inside the reviewed workspace are
    changed. Task trajectories and supplied symbolic snapshot IDs stay exact.
    Raw model output is retained separately by the controller.
    """
    root = workspace.resolve()
    mappings = {}
    def reference(ref):
        if not isinstance(ref, str):
            return ref
        text = ref.removeprefix("code:")
        suffix = re.search(r":\d+(?::\d+)?$", text)
        location = suffix.group() if suffix else ""
        path = Path(text[:-len(location)] if location else text)
        if not path.is_absolute():
            return ref
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return ref
        replacement = "code:" + resolved.relative_to(root).as_posix() + location
        mappings[ref] = replacement
        return replacement
    def visit(value):
        if isinstance(value, dict):
            return {key: [reference(ref) for ref in child] if key in {"evidence", "source_evidence", "execution_evidence", "source_inspected", "execution_inspected", "tests_inspected", "inspected"} and isinstance(child, list)
                    else reference(child) if key == "reference" and isinstance(child, str)
                    else visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        return value
    canonical_result = visit(result)
    # A technical design's JSON index also lives inside its frozen Markdown.
    # Rebase that index together with the parsed metadata, retaining the raw
    # model document separately and the existing citation provenance mapping.
    from alpha_agents.tree_search.core.mcts.design_documents import (
        FORMAT,
        parse_design_document,
    )
    if canonical_result.get("plan_format") == FORMAT:
        for original_plan, plan in zip(result["plans"], canonical_result["plans"]):
            before = parse_design_document(original_plan["document"])
            after = visit(before)
            if before != after:
                plan["document"] = re.sub(
                    r"(^```json\s*\n).*?(^```\s*$)",
                    lambda match: match.group(1) + json.dumps(after, indent=2) + "\n" + match.group(2),
                    original_plan["document"], count=1, flags=re.MULTILINE | re.DOTALL)
                if result.get("native_document") == original_plan["document"]:
                    canonical_result["native_document"] = plan["document"]
    return canonical_result, [{"original": original, "canonical": canonical}
                              for original, canonical in sorted(mappings.items())]


def validate_response(result: dict, request: RoleRequest, workspace: Path) -> dict:
    if result.get("artifact_id") != request.artifact_id:
        raise ValueError("Response artifact identity mismatch")
    role = request.role
    if role == "proposer":
        from alpha_agents.tree_search.core.mcts.design_documents import (
            FORMAT,
            parse_design_document,
            validate_summary,
        )
        if request.context.get("plan_format") == FORMAT:
            plans = result.get("plans")
            if result.get("plan_format") != FORMAT or not isinstance(plans, list) or len(plans) != 1:
                raise ValueError("Proposer must return exactly one technical design document")
            plan = plans[0]
            validate_summary({key: value for key, value in plan.items() if key != "document"})
            if not isinstance(plan.get("document"), str) or not plan["document"].strip():
                raise ValueError("Technical design document is required")
            if parse_design_document(plan["document"]) != {key: value for key, value in plan.items() if key != "document"}:
                raise ValueError("Plan JSON summary differs from its technical design document")
            _evidence(plan["evidence"], request, workspace)
            if any(normalize_intervention(plan["intervention"]) == normalize_intervention(existing["intervention"])
                   for existing in request.context.get("existing_plans", [])):
                raise ValueError("Proposed intervention duplicates an existing plan at this node")
            return result
        plans, archive = result.get("plans"), result.get("archive_review")
        if not isinstance(plans, list) or len(plans) > 3 or not isinstance(archive, dict):
            raise ValueError("Invalid proposed plan/archive records")
        required_count = request.context.get("required_plan_count")
        if required_count is not None and len(plans) != required_count:
            raise ValueError("Expansion requires exactly three distinct plans")
        catalog = set(request.context["plan_archive"]["plan_ids"])
        if set(archive.get("catalog_plan_ids", [])) != catalog or archive.get("gaps"):
            raise ValueError("Proposal must review the complete current plan catalog without access gaps")
        if not set(archive.get("inspected_plan_ids", [])) <= catalog:
            raise ValueError("Archive inspection references unknown plans")
        if not plans and not result.get("zero_reason"):
            raise ValueError("Zero-plan proposal requires an investigated reason")
        if len({p["id"] for p in plans}) != len(plans):
            raise ValueError("Candidate IDs must be distinct")
        for p in plans:
            if request.context.get("novelty_admission_contract"):
                status = p.get("novelty_status")
                if status not in {"new_action", "cross_parent_adaptation"}:
                    raise ValueError("Proposal requires an admissible explicit novelty classification")
                if declares_same_parent_duplicate(p):
                    raise ValueError("Candidate novelty explanation declares a same-parent duplicate")
                if status == "cross_parent_adaptation":
                    parents = {entry["id"]: entry.get("parent_artifact")
                               for entry in request.context["plan_archive"].get("plans", [])}
                    closest = p.get("closest_plan_ids", [])
                    if not closest or not any(parents.get(id) and parents[id] != request.artifact_id for id in closest):
                        raise ValueError("Cross-parent adaptation requires a reviewed plan at a different parent")
            if not all(p.get(key) for key in ("document", "novelty", "diversity_audit", "intervention")):
                raise ValueError("Incomplete detailed candidate plan")
            normalize_intervention(p["intervention"])
            if not set(p.get("closest_plan_ids", [])) <= catalog:
                raise ValueError("Novelty comparison references unknown archive plans")
            _evidence(p.get("evidence"), request, workspace)
    elif role == "policy":
        plans = {p["id"] for p in request.context["plans"]}
        if result.get("evidence_snapshot_id") != request.context["evidence_snapshot_id"] or set(result.get("plan_ids", [])) != plans:
            raise ValueError("Policy evidence/plan identity mismatch")
        raw = result.get("priors", {})
        if set(raw) != plans:
            raise ValueError("Policy must cover every frozen plan exactly once")
        prepare_priors(raw)
        if not result.get("comparisons") or not result.get("uncertainty"):
            raise ValueError("Policy requires developed comparisons and uncertainty")
        _evidence(result.get("evidence"), request, workspace)
    elif role == "implementer":
        if result.get("plan_id") != request.context["plan_id"] or result.get("attempt_id") != request.context["attempt_id"]:
            raise ValueError("Implementation plan/attempt identity mismatch")
        if result.get("status") not in {"completed", "partial", "failed"}:
            raise ValueError("Unknown implementation status")
        verification = result.get("verification")
        if not isinstance(verification, list) or not isinstance(result.get("blockers"), list):
            raise ValueError("Implementation requires actual check records and blocker list")
        for check in verification:
            if not isinstance(check.get("ran"), bool) or check.get("status") not in {"completed", "failed", "not_run"}:
                raise ValueError("Verification must distinguish executed from requested checks")
            if check["status"] == "completed" and not check["ran"]:
                raise ValueError("An unrun check cannot be represented as a success")
            if check["ran"]:
                _evidence(check.get("evidence"), request, workspace)
        if result["status"] == "completed":
            if not result.get("changes") or result["blockers"] or not verification or not all(c["ran"] and c["status"] == "completed" for c in verification):
                raise ValueError("Completed implementation needs assigned changes and meaningful completed checks")
        _evidence(result.get("evidence"), request, workspace)
    elif role == "judge_review":
        from alpha_agents.tree_search.core.mcts.judge_review import validate_review
        if (result.get("rubric_version") != request.context["rubric"]
                or result.get("canonical_evidence_id") != request.context["canonical_evidence"]["snapshot_id"]):
            raise ValueError("Unified judge rubric/canonical evidence identity mismatch")
        validate_review(result, lambda refs: _evidence(refs, request, workspace))
    elif role.startswith("judge_"):
        phase = "code" if role == "judge_code" else "execution"
        if result.get("phase") != phase or result.get("rubric_version") != request.context["rubric"]:
            raise ValueError("Judge phase/rubric identity mismatch")
        if "plan_alignment" in result or "parent_score" in result:
            raise ValueError("Judge output contains an excluded score dimension")
        if not result.get("findings") or not isinstance(result.get("coverage"), dict):
            raise ValueError("Judge requires findings and explicit inspected/unseen coverage")
        if phase == "execution":
            if result.get("status") != "completed" or result.get("canonical_evidence_id") != request.context["canonical_evidence"]["snapshot_id"]:
                raise ValueError("Final judge is incomplete or identifies different canonical evidence")
            judge_aggregate(result.get("dimensions", {}))
            for item in result["dimensions"].values():
                if item["score"] is not None:
                    _evidence(item["evidence"], request, workspace)
    elif role == "value":
        from alpha_agents.tree_search.core.mcts.value_forecast import validate_forecast
        validate_forecast(result, request.context, request.artifact_id,
                          lambda refs: _evidence(refs, request, workspace))
        if result["status"] != "completed":
            raise ValueError("Frontier value assessment is incomplete; preserve the pending trial")
    return result
