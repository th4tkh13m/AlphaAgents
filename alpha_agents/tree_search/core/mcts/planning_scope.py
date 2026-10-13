"""Versioned planning context for implementable verification scopes."""

import re


def declares_same_parent_duplicate(candidate):
    # Catch a direct self-declaration observed in live output even when the
    # status label contradicts it. This is not a semantic similarity detector.
    return bool(
        re.match(
            r"^\s*same[- ]parent duplicate (?:of|with)\b",
            str(candidate.get("novelty", "")),
            re.IGNORECASE,
        )
    )


def verification_scope_contract():
    return {"version": "test-edit-scope-v1"}


def novelty_admission_contract():
    return {
        "version": "novelty-admission-v2",
        "required_candidate_field": "novelty_status",
        "admissible_statuses": ["new_action", "cross_parent_adaptation"],
    }
