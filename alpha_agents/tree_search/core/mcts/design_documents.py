"""Shared contract for one technical design document and its JSON index."""
from __future__ import annotations

FORMAT = "technical-design-v1"
SECTIONS = ("Problem and motivation", "Current behavior and evidence", "Prior attempts",
            "Proposed design", "Implementation plan", "Validation", "Risks and limitations")
STRING_FIELDS = ("id", "title", "summary", "expected_benefit", "validation", "risks_and_limitations")
SUMMARY_FIELDS = {*STRING_FIELDS, "intervention", "evidence"}


def validate_summary(summary: dict):
    if not isinstance(summary, dict) or set(summary) != SUMMARY_FIELDS:
        raise ValueError("Technical design summary must contain exactly the required fields")
    if any(not isinstance(summary[key], str) or not summary[key].strip() for key in STRING_FIELDS):
        raise ValueError("Technical design summary requires nonempty descriptive strings")
    intervention = summary["intervention"]
    if (not isinstance(intervention, dict)
            or set(intervention) != {"target", "operation", "conditions", "mechanism"}
            or any(not isinstance(value, str) or not value.strip() for value in intervention.values())):
        raise ValueError("Technical design intervention requires four nonempty strings")
    refs = summary["evidence"]
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("Technical design evidence must be a nonempty reference array")


def parse_design_document(document: str) -> dict:
    """Validate the complete Markdown handoff independently of role provider."""
    import json
    import re

    if not isinstance(document, str) or not document.strip():
        raise ValueError("Technical design document is required")
    sections = []
    fence = None
    offset = 0
    for line in document.splitlines(keepends=True):
        stripped = line.strip()
        match = re.match(r"^(`{3,}|~{3,})", stripped)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = None
        elif fence is None and line.startswith("## "):
            sections.append((stripped[3:].strip(), offset))
        offset += len(line)
    if fence is not None:
        raise ValueError("Technical design contains an unclosed code fence")
    if tuple(heading for heading, _ in sections) != SECTIONS:
        raise ValueError("Technical design must contain the seven required sections in order")
    preamble = document[:sections[0][1]]
    blocks = re.findall(r"^```json\s*\n(.*?)^```\s*$", preamble, re.MULTILINE | re.DOTALL)
    if len(blocks) != 1 or len(re.findall(r"^```json\s*$", document, re.MULTILINE)) != 1:
        raise ValueError("Technical design requires exactly one JSON summary before its sections")
    summary = json.loads(blocks[0])
    validate_summary(summary)
    title = re.match(r"\s*# ([^\n]+)\n", preamble)
    if title is None or title.group(1).strip() != summary["title"]:
        raise ValueError("Technical design title must match its JSON summary")
    for index, (heading, start) in enumerate(sections):
        end = sections[index+1][1] if index+1 < len(sections) else len(document)
        if not document[start:end].removeprefix("## " + heading).strip():
            raise ValueError(f"Technical design section is empty: {heading}")
    return summary
