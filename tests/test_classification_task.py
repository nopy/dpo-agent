"""Tests for the clause_classification task.

The clause_classification task is the fourth built-in task. It
takes a contract and a taxonomy (caller-provided list of
labels) and produces a list of classifications — one per
substantive clause, with one or more labels and a confidence
score.

Key differences from other tasks:
- The schema parameter is a TAXONOMY (list of labels), not a
  JSON Schema.
- Multi-label assignment is expected and normal.
- The output distinguishes "substantive clauses" from
  "unclassified_chunks" (boilerplate, TOC, signatures).
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_classification_task_is_listed():
    tasks = list_tasks()
    assert "clause_classification" in tasks
    assert "dpo" in tasks
    assert "metadata" in tasks
    assert "redline_suggest" in tasks


def test_classification_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("clause_classification", prompt_type)
        assert len(prompt) > 500


def test_classification_reviewer_mentions_taxonomy():
    """The reviewer prompt should reference the taxonomy concept
    explicitly — it's the label source for classifications."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "taxonomy" in prompt.lower()
    assert "classification" in prompt.lower() or "classify" in prompt.lower()


def test_classification_reviewer_supports_both_taxonomy_formats():
    """The reviewer should support both:
    - simple list of labels: ["indemnification", ...]
    - rich list with descriptions: [{"label": ..., "description": ...}]
    """
    prompt = load_prompt("clause_classification", "reviewer")
    # Both formats should be mentioned
    assert '"label"' in prompt or "'label'" in prompt
    assert '"description"' in prompt or "'description'" in prompt
    assert "examples" in prompt.lower()


def test_classification_reviewer_output_schema():
    """The reviewer should produce JSON with classifications,
    labels_used, unclassified_chunks, open_questions."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "classifications" in prompt
    assert "labels_used" in prompt
    assert "unclassified_chunks" in prompt
    assert "open_questions" in prompt


def test_classification_reviewer_supports_multi_label():
    """The reviewer must explicitly say a clause can have multiple
    labels. This is a defining feature of clause classification
    (vs. single-label classification)."""
    prompt = load_prompt("clause_classification", "reviewer")
    lower = prompt.lower()
    # Should say "one or more" or "multi-label" or "multiple labels"
    assert "one or more" in lower or "multiple" in lower or "multi-label" in lower


def test_classification_reviewer_distinguishes_substantive_from_boilerplate():
    """A defining feature: the task must distinguish substantive
    clauses (the things being classified) from boilerplate (cover
    page, TOC, signatures, definitions, notices)."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "substantive" in prompt.lower()
    # Common boilerplate to call out
    lower = prompt.lower()
    assert "cover page" in lower
    assert "table of contents" in lower or "toc" in lower
    assert "signature" in lower


def test_classification_reviewer_invent_labels_discipline():
    """The prompt should explicitly forbid inventing labels —
    labels must come from the taxonomy."""
    prompt = load_prompt("clause_classification", "reviewer")
    lower = prompt.lower()
    assert "never invent" in lower or "do not invent" in lower
    # The "unclassified" mechanism is the safety valve
    assert "unclassified" in lower


def test_classification_reviewer_per_label_rationale():
    """The reviewer should require a one-sentence rationale per
    label, so the human verifier can quickly understand the
    assignment without re-reading the clause."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "rationale" in prompt.lower()


def test_classification_reviewer_citation_format():
    """Every classification must cite both the section reference
    AND the chunk index."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "section_ref" in prompt
    assert "chunk" in prompt


def test_classification_reviewer_coverage_metric():
    """The reviewer should compute taxonomy_coverage =
    labels_used / total_labels. This tells the caller how much
    of the taxonomy was actually seen in the contract."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "taxonomy_coverage" in prompt
    assert "labels_not_used" in prompt


def test_classification_critique_5_axes():
    """The critique prompt should cover the 5 axes:
    1. Grounding of clause_text
    2. Grounding of labels (must be in taxonomy)
    3. Completeness (every clause classified)
    4. Confidence calibration
    5. Open questions (taxonomy gaps)"""
    prompt = load_prompt("clause_classification", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "completeness" in lower
    assert "confidence" in lower
    assert "open question" in lower or "open_question" in lower
    # The taxonomy check is unique to this task
    assert "taxonomy" in lower


def test_classification_navigator_packet_schema():
    """The navigator's packet should have two sections:
    substantive clauses (one per section) and non-substantive
    chunks (TOC, signatures, etc.)."""
    prompt = load_prompt("clause_classification", "navigator")
    assert "substantive" in prompt.lower()
    assert "non-substantive" in prompt.lower() or "boilerplate" in prompt.lower()
    assert "section_ref" in prompt
    assert "clause_text" in prompt


def test_classification_navigator_groups_by_section():
    """The navigator should group reads by section proximity
    (similar to redline_suggest's grouping by clause type)."""
    prompt = load_prompt("clause_classification", "navigator")
    assert "group" in prompt.lower()


def test_classification_prompts_distinct_from_others():
    """The four task prompts should all be different content."""
    dpo = load_prompt("dpo", "reviewer")
    metadata = load_prompt("metadata", "reviewer")
    redline = load_prompt("redline_suggest", "reviewer")
    classification = load_prompt("clause_classification", "reviewer")
    assert len({dpo, metadata, redline, classification}) == 4
    # Classification-specific content
    assert "taxonomy" in classification.lower()
    assert "labels" in classification.lower()
    # Sanity: dpo doesn't mention "taxonomy" (its schema is the
    # 42-item GDPR checklist, not a taxonomy)
    assert "taxonomy" not in dpo.lower() or "taxonomy" not in dpo


def test_classification_reviewer_confidence_when_in_doubt():
    """When confidence is ambiguous, the prompt should default
    to the LOWER confidence — the human verifier can upgrade
    but not easily downgrade after a quick scan."""
    prompt = load_prompt("clause_classification", "reviewer")
    assert "downgrade" in prompt.lower() or "lower" in prompt.lower()


def test_classification_reviewer_no_label_better_than_wrong_label():
    """The reviewer should explicitly say it's better to assign
    no label than to assign a wrong one — wrong labels are
    worse than missing labels."""
    prompt = load_prompt("clause_classification", "reviewer")
    lower = prompt.lower()
    assert "prefer no label" in lower or "no label over" in lower or \
           "wrong labels are worse" in lower or "missing labels" in lower
