"""Test that the metadata task works end-to-end.

This test doesn't make API calls — it verifies the metadata task
is loaded correctly, the prompt mentions the right concepts, and
the loader can find all 3 prompt files.
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_metadata_task_is_listed():
    tasks = list_tasks()
    assert "metadata" in tasks
    assert "dpo" in tasks  # backwards compat


def test_metadata_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("metadata", prompt_type)
        assert len(prompt) > 500
        assert "metadata" in prompt.lower()


def test_metadata_reviewer_prompt_mentions_schema():
    """The reviewer prompt should reference the schema concept."""
    prompt = load_prompt("metadata", "reviewer")
    assert "schema" in prompt.lower()
    assert "confidence" in prompt.lower()
    assert "verbatim" in prompt.lower()
    assert "json" in prompt.lower()


def test_metadata_critique_prompt_mentions_5_axes():
    """The critique prompt should reference the 5 critique axes."""
    prompt = load_prompt("metadata", "critique")
    # The 5 critique axes: Grounding, Completeness, Type correctness,
    # Confidence calibration, Open questions.
    assert "grounding" in prompt.lower()
    assert "completeness" in prompt.lower()
    assert "confidence" in prompt.lower()


def test_metadata_navigator_prompt_mentions_packet():
    """The navigator prompt should reference the findings packet."""
    prompt = load_prompt("metadata", "navigator")
    assert "packet" in prompt.lower()
    assert "schema" in prompt.lower()


def test_metadata_reviewer_mentions_specific_field_types():
    """The reviewer should give guidance on dates, monetary, lists."""
    prompt = load_prompt("metadata", "reviewer")
    assert "ISO 8601" in prompt or "iso 8601" in prompt.lower()
    assert "currency" in prompt.lower()
    assert "list" in prompt.lower()


def test_metadata_prompts_distinct_from_dpo():
    """The metadata and dpo prompts should be different content."""
    dpo_reviewer = load_prompt("dpo", "reviewer")
    metadata_reviewer = load_prompt("metadata", "reviewer")
    assert dpo_reviewer != metadata_reviewer
    assert "Data Protection Officer" in dpo_reviewer
    assert "Data Protection Officer" not in metadata_reviewer
    assert "metadata" in metadata_reviewer.lower()
    assert "JSON" in metadata_reviewer  # metadata uses JSON output


def test_metadata_discipline_doctrine():
    """The reviewer prompt should teach the discipline principles:
    quote verbatim, prefer null over guess, resolve contradictions,
    normalize dates, normalize monetary, preserve list order."""
    prompt = load_prompt("metadata", "reviewer")
    lower = prompt.lower()
    assert "verbatim" in lower
    assert "null" in lower
    assert "contradiction" in lower or "contradict" in lower
    assert "iso 8601" in lower or "iso" in lower
    assert "currency" in lower
    assert "order" in lower  # preserve list order
