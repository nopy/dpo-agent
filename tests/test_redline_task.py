"""Tests for the redline_suggest task.

The redline_suggest task is the third built-in task. It compares
a contract against a playbook (firm's preferred language) and
proposes redlines for clauses that deviate. The playbook is
passed to the agent via the `schema` parameter (because it's
the comparison source, not a fixed label set).
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_redline_task_is_listed():
    tasks = list_tasks()
    assert "redline_suggest" in tasks
    assert "dpo" in tasks
    assert "metadata" in tasks


def test_redline_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("redline_suggest", prompt_type)
        assert len(prompt) > 500


def test_redline_reviewer_mentions_playbook():
    """The reviewer prompt should reference the playbook concept
    explicitly — it's the comparison source for redlines."""
    prompt = load_prompt("redline_suggest", "reviewer")
    assert "playbook" in prompt.lower()
    assert "redline" in prompt.lower()
    assert "preferred_language" in prompt
    assert "fallback_language" in prompt
    assert "red_flags" in prompt


def test_redline_reviewer_output_schema():
    """The reviewer should produce a JSON with executive_summary,
    matching_clauses, proposed_redlines, and open_questions."""
    prompt = load_prompt("redline_suggest", "reviewer")
    assert "executive_summary" in prompt
    assert "matching_clauses" in prompt
    assert "proposed_redlines" in prompt
    assert "open_questions" in prompt


def test_redline_reviewer_invent_language_discipline():
    """The prompt should explicitly forbid inventing redline
    language — the agent must use the playbook's exact wording."""
    prompt = load_prompt("redline_suggest", "reviewer")
    lower = prompt.lower()
    assert "never invent" in lower or "do not invent" in lower
    assert "verbatim" in lower
    assert "playbook" in lower  # the source of truth


def test_redline_reviewer_severity_levels():
    """The reviewer should define the 4 severity levels: critical,
    high, medium, low (info is a bonus)."""
    prompt = load_prompt("redline_suggest", "reviewer")
    assert "critical" in prompt.lower()
    assert "high" in prompt.lower()
    assert "medium" in prompt.lower()
    assert "low" in prompt.lower()


def test_redline_reviewer_does_not_redline_matching_clauses():
    """A specific discipline: don't propose redlines for clauses
    that already match the playbook. The output schema has a
    separate matching_clauses section for this."""
    prompt = load_prompt("redline_suggest", "reviewer")
    assert "matching_clauses" in prompt
    # Should explicitly say "don't propose" matching clauses
    lower = prompt.lower()
    assert "don't propose" in lower or "do not propose" in lower


def test_redline_critique_mentions_5_axes():
    """The critique prompt should reference the 5 critique axes."""
    prompt = load_prompt("redline_suggest", "critique")
    lower = prompt.lower()
    # The 5 critique axes (from the metadata task's critique,
    # adapted for redlines):
    # 1. Grounding — current_text
    # 2. Grounding — proposed_text
    # 3. Completeness
    # 4. Severity calibration
    # 5. Open questions
    assert "grounding" in lower
    assert "completeness" in lower
    assert "severity" in lower
    assert "current_text" in lower or "current text" in lower
    assert "proposed_text" in lower or "proposed text" in lower


def test_redline_navigator_packet_schema():
    """The navigator's packet should be organized by playbook
    clause types, with present/chunks/section_refs/verbatim
    per type."""
    prompt = load_prompt("redline_suggest", "navigator")
    assert "clause_type" in prompt
    assert "present" in prompt
    assert "verbatim" in prompt.lower()
    assert "section_refs" in prompt
    assert "playbook" in prompt.lower()


def test_redline_navigator_groups_clause_types_by_section():
    """The navigator prompt should teach the model to group clause
    types by section to minimize chunk reads."""
    prompt = load_prompt("redline_suggest", "navigator")
    # Look for the "group" pattern.
    assert "group" in prompt.lower()
    # Specific groupings mentioned in the prompt.
    assert "indemnification" in prompt.lower()
    assert "limitation" in prompt.lower()


def test_redline_prompts_distinct_from_dpo_and_metadata():
    """Each task's prompts are different content."""
    dpo = load_prompt("dpo", "reviewer")
    metadata = load_prompt("metadata", "reviewer")
    redline = load_prompt("redline_suggest", "reviewer")
    assert dpo != metadata != redline
    assert dpo != redline
    # Sanity: the dpo task mentions "GDPR", redline mentions
    # "playbook" or "redline".
    assert "GDPR" in dpo
    assert "playbook" in redline.lower()
    assert "redline" in redline.lower()


def test_redline_reviewer_severity_calibration_when_in_doubt():
    """When severity is ambiguous, the prompt should default to
    the higher severity — the human can downgrade but not
    upgrade after a skim."""
    prompt = load_prompt("redline_suggest", "reviewer")
    assert "downgrade" in prompt.lower()
    assert "upgrade" in prompt.lower() or "in doubt" in prompt.lower()
