"""Tests for the summarize task.

The summarize task is the fifth built-in task. It produces a
4-section executive summary (TL;DR, Key Terms, Risks, Open
Questions, plus Parties-and-Term for contracts or
Methodology-and-Findings for research papers).

Key differences from other tasks:
- The output is structured markdown, not JSON.
- The schema parameter is OPTIONAL (audience, target_length,
  focus_areas are hints, not a contract).
- The task teaches the discipline of NOT inventing details
  and NOT summarizing what wasn't read.
- Risks are the only task that has severity calibration
  outside of redline_suggest.
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_summarize_task_is_listed():
    tasks = list_tasks()
    assert "summarize" in tasks
    assert len(tasks) >= 5  # was 5, may grow as tasks are added


def test_summarize_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("summarize", prompt_type)
        assert len(prompt) > 500


def test_summarize_reviewer_4_sections():
    """The reviewer should produce 4 sections: TL;DR, Key Terms,
    Risks / Concerns, Open Questions."""
    prompt = load_prompt("summarize", "reviewer")
    assert "TL;DR" in prompt
    assert "Key Terms" in prompt
    assert "Risks" in prompt or "Concerns" in prompt
    assert "Open Questions" in prompt


def test_summarize_reviewer_optional_5th_section():
    """For contracts, the 5th section is 'Parties and Term'. For
    research papers, 'Methodology and Findings'. The prompt
    should teach both shapes."""
    prompt = load_prompt("summarize", "reviewer")
    assert "Parties and Term" in prompt
    assert "Methodology and Findings" in prompt


def test_summarize_reviewer_optional_context_hints():
    """The reviewer should accept audience, target_length,
    focus_areas, and document_type_hint as optional hints."""
    prompt = load_prompt("summarize", "reviewer")
    assert "audience" in prompt.lower()
    assert "target_length" in prompt
    assert "focus_areas" in prompt
    assert "document_type_hint" in prompt


def test_summarize_reviewer_output_is_markdown_not_json():
    """Unlike the other 4 tasks, summarize produces markdown.
    The prompt must explicitly say so."""
    prompt = load_prompt("summarize", "reviewer")
    assert "markdown" in prompt.lower()
    # Should explicitly distinguish from JSON
    assert "not json" in prompt.lower() or "structured markdown" in prompt.lower()


def test_summarize_reviewer_invent_discipline():
    """The prompt must explicitly forbid inventing details —
    this is the #1 failure mode for summary agents."""
    prompt = load_prompt("summarize", "reviewer")
    lower = prompt.lower()
    assert "never invent" in lower or "do not invent" in lower
    # Specifically: no invented numbers, no invented parties
    assert "no invented" in lower or "no invented numbers" in lower


def test_summarize_reviewer_no_summarize_what_you_didnt_read():
    """The prompt must explicitly say: don't summarize what you
    didn't read. A summary agent that pretends to have read
    every section is misleading."""
    prompt = load_prompt("summarize", "reviewer")
    lower = prompt.lower()
    assert "didn" in lower or "did not" in lower or "didn't" in lower
    # The phrase "didn't read" should appear
    assert "what you didn't read" in lower or "didn't read" in lower or \
           "what you did not read" in lower


def test_summarize_reviewer_severity_calibration():
    """The reviewer should define the 5 severity levels: critical,
    high, medium, low, info."""
    prompt = load_prompt("summarize", "reviewer")
    assert "critical" in prompt.lower()
    assert "high" in prompt.lower()
    assert "medium" in prompt.lower()
    assert "low" in prompt.lower()
    assert "info" in prompt.lower()


def test_summarize_reviewer_default_length():
    """The reviewer should give a default length target so the
    agent doesn't accidentally produce a 5-page summary."""
    prompt = load_prompt("summarize", "reviewer")
    # Should mention target word counts
    assert "500" in prompt or "700" in prompt or "300" in prompt or "word" in prompt.lower()


def test_summarize_reviewer_cite_every_claim():
    """The reviewer should require citations on every claim."""
    prompt = load_prompt("summarize", "reviewer")
    assert "cite" in prompt.lower() or "section" in prompt.lower()


def test_summarize_reviewer_verbatim_when_phrasing_matters():
    """The reviewer should teach: quote verbatim when phrasing
    matters (especially for legal language)."""
    prompt = load_prompt("summarize", "reviewer")
    assert "verbatim" in prompt.lower()


def test_summarize_navigator_risk_indicator_phrases():
    """The NAVIGATOR should give examples of risk-bearing language
    in the packet — 'uncapped', 'sole discretion', etc. The
    navigator extracts these from the source; the reviewer uses
    them to write the Risks section."""
    prompt = load_prompt("summarize", "navigator")
    assert "uncapped" in prompt.lower()
    assert "sole discretion" in prompt.lower()


def test_summarize_reviewer_prefer_no_bullet():
    """The reviewer should teach: prefer no bullet over a
    wrong bullet. Quality > quantity in summaries."""
    prompt = load_prompt("summarize", "reviewer")
    assert "no bullet" in prompt.lower() or "drop" in prompt.lower() or \
           "wrong" in prompt.lower()


def test_summarize_critique_5_axes():
    """The critique prompt should cover the 5 axes:
    1. Grounding (no invented details)
    2. Citation (every claim cited)
    3. Completeness
    4. Severity calibration
    5. Length discipline
    """
    prompt = load_prompt("summarize", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "citation" in lower
    assert "completeness" in lower
    assert "severity" in lower
    assert "length" in lower


def test_summarize_critique_invented_check():
    """The critique prompt should specifically check for invented
    details: numbers, parties, dates that aren't in the source."""
    prompt = load_prompt("summarize", "critique")
    lower = prompt.lower()
    # The check: if a number, party, or date is in the summary
    # but not the source, you invented it.
    assert "invent" in lower or "invention" in lower or "in the source" in lower


def test_summarize_navigator_packet_schema():
    """The navigator's packet should be organized by the 4 (or
    5) summary sections, with verbatim excerpts and
    risk_indicator_phrases for the Risks section."""
    prompt = load_prompt("summarize", "navigator")
    # The packet is organized by the summary sections
    assert "Key Terms" in prompt
    assert "Risks" in prompt or "Concerns" in prompt
    # The risk_indicator_phrases field is unique to this navigator
    assert "risk_indicator_phrases" in prompt or "risk indicator" in prompt.lower()


def test_summarize_navigator_silence_identification():
    """The navigator should explicitly look for what's MISSING
    in the document, not just what's there. 'Open Questions'
    comes from gaps in the document."""
    prompt = load_prompt("summarize", "navigator")
    assert "silent" in prompt.lower() or "missing" in prompt.lower() or "gap" in prompt.lower()


def test_summarize_prompts_distinct_from_others():
    """The 5 task prompts should all be different content."""
    dpo = load_prompt("dpo", "reviewer")
    metadata = load_prompt("metadata", "reviewer")
    redline = load_prompt("redline_suggest", "reviewer")
    classification = load_prompt("clause_classification", "reviewer")
    summarize = load_prompt("summarize", "reviewer")
    assert len({dpo, metadata, redline, classification, summarize}) == 5


def test_summarize_reviewer_no_schema_required():
    """Unlike other tasks, summarize doesn't require a schema
    parameter. The prompt should say so."""
    prompt = load_prompt("summarize", "reviewer")
    # The context block lists audience, target_length, etc.
    # as OPTIONAL.
    assert "optional" in prompt.lower()


def test_summarize_reviewer_markdown_section_headers():
    """The output markdown should use the exact section headers
    the calling code parses: '## TL;DR', '## Key Terms',
    '## Risks / Concerns', '## Open Questions'."""
    prompt = load_prompt("summarize", "reviewer")
    assert "## TL;DR" in prompt
    assert "## Key Terms" in prompt
    assert "## Risks / Concerns" in prompt
    assert "## Open Questions" in prompt
