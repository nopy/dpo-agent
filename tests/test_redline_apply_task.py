"""Tests for the redline_apply task.

The redline_apply task is the seventh built-in task. It takes
the output of redline_suggest (a redline package) and applies
it to a source contract, producing a redlined document +
change log.

Key differences from other tasks:
- The output TRANSFORMS the source contract (substitutes
  proposed_text for current_text). All other tasks extract
  or classify; this one mutates.
- The input is a redline package, not a contract from the
  source store. The agent reads the contract via document
  tools but the redline package comes via the schema param.
- The output has 5 blocks (executive_summary, redlined_document,
  change_log, unapplied_redlines, suggested_additional_redlines)
  vs the 4 in redline_suggest.
- The discipline includes a grammar / consistency check
  (voice, tense, defined terms) that other tasks don't have.
- A redline that can't be applied isn't silently dropped;
  it goes to unapplied_redlines with a reason.
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_redline_apply_task_is_listed():
    tasks = list_tasks()
    assert "redline_apply" in tasks
    assert len(tasks) >= 7  # was 7, may grow as tasks are added
    # Both redline_suggest and redline_apply should be present
    assert "redline_suggest" in tasks


def test_redline_apply_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("redline_apply", prompt_type)
        assert len(prompt) > 500


def test_redline_apply_reviewer_input_is_redline_package():
    """The reviewer prompt should reference the redline package
    as input — it comes from redline_suggest."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "redline_package" in prompt.lower() or "redline package" in prompt.lower()
    assert "proposed_redlines" in prompt


def test_redline_apply_reviewer_output_5_blocks():
    """The reviewer should produce 5 blocks: executive_summary,
    redlined_document, change_log, unapplied_redlines,
    suggested_additional_redlines."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "executive_summary" in prompt
    assert "redlined_document" in prompt
    assert "change_log" in prompt
    assert "unapplied_redlines" in prompt
    assert "suggested_additional_redlines" in prompt


def test_redline_apply_reviewer_never_invent_text():
    """The agent must never invent text. Every change must come
    from a proposed_text in the package. This is the #1
    failure mode for redline apply."""
    prompt = load_prompt("redline_apply", "reviewer")
    lower = prompt.lower()
    assert "never invent" in lower or "do not invent" in lower


def test_redline_apply_reviewer_never_silently_drop():
    """Every redline in the package must be applied, rejected,
    or flagged for review. Silently dropping is forbidden."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "silently" in prompt.lower() or "silent drop" in prompt.lower() or \
           "fourth option" in prompt.lower()


def test_redline_apply_reviewer_match_current_text_exactly():
    """The agent must match current_text in the source before
    substituting proposed_text. If not found, the redline
    is unapplied (not guessed)."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "current_text" in prompt
    assert "match" in prompt.lower() or "exact" in prompt.lower()


def test_redline_apply_reviewer_grammar_consistency_check():
    """The agent must check voice, tense, defined terms
    consistency after substituting. This is unique to
    redline_apply (other tasks don't mutate text)."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "grammar" in prompt.lower() or "voice" in prompt.lower()
    assert "tense" in prompt.lower()
    assert "defined term" in prompt.lower() or "defined_term" in prompt.lower()


def test_redline_apply_reviewer_apply_modes():
    """The agent should accept an apply_mode (strict / fuzzy /
    preview) — different matching strategies."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "strict" in prompt.lower()
    assert "fuzzy" in prompt.lower()
    assert "preview" in prompt.lower()


def test_redline_apply_reviewer_track_changes_formats():
    """The agent should accept track_changes format (brackets /
    tracked / clean) for inline change markers."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "brackets" in prompt.lower()
    assert "tracked" in prompt.lower()
    assert "clean" in prompt.lower()


def test_redline_apply_reviewer_status_field():
    """Every change_log entry has a status: applied /
    rejected / requires_human_review."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "applied" in prompt
    assert "rejected" in prompt
    assert "requires_human_review" in prompt or "human_review" in prompt.lower()


def test_redline_apply_reviewer_substitute_in_document_order():
    """Substitution must be in document order to avoid
    accidentally substituting into already-modified text."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "document order" in prompt.lower() or "order" in prompt.lower()


def test_redline_apply_reviewer_preserve_unrelated_text():
    """Every paragraph NOT targeted by a redline must appear
    verbatim in the redlined_document. The agent edits
    specific clauses, not the whole document."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "preserve" in prompt.lower() or "verbatim" in prompt.lower()


def test_redline_apply_reviewer_no_combining_overlapping_redlines():
    """If two redlines target overlapping current_text, apply
    the first and reject the second. Don't combine them."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "overlap" in prompt.lower() or "combine" in prompt.lower()


def test_redline_apply_reviewer_honest_about_unapplied():
    """A redline that doesn't match the source is a problem
    with the redline, not the source. Surface it; don't
    pretend it applied."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "honest" in prompt.lower() or "pretend" in prompt.lower()


def test_redline_apply_reviewer_risk_reduction_estimate():
    """The executive_summary should include a risk reduction
    estimate (the change in score if all redlines are
    accepted)."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "risk_reduction" in prompt or "risk reduction" in prompt.lower()


def test_redline_apply_critique_5_axes():
    """The critique prompt should cover the 5 axes:
    1. Grounding (current_text match)
    2. Substitution correctness (proposed_text)
    3. Completeness (every redline in change_log or
       unapplied_redlines)
    4. Document preservation
    5. Grammar / consistency re-check
    """
    prompt = load_prompt("redline_apply", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "substitution" in lower
    assert "completeness" in lower
    assert "preservation" in lower or "preserve" in lower
    assert "grammar" in lower or "consistency" in lower


def test_redline_apply_critique_doc_preservation():
    """The critique prompt should specifically check that
    unrelated paragraphs are preserved verbatim. This is the
    #1 failure mode for redline applicators — accidentally
    paraphrasing text outside the redline scope."""
    prompt = load_prompt("redline_apply", "critique")
    assert "preserve" in prompt.lower() or "verbatim" in prompt.lower()


def test_redline_apply_critique_proposed_text_match():
    """The critique prompt should verify that every applied
    redline's proposed_text is the verbatim text from the
    package, not a paraphrase."""
    prompt = load_prompt("redline_apply", "critique")
    assert "proposed_text" in prompt
    assert "invented" in prompt.lower() or "verbatim" in prompt.lower()


def test_redline_apply_navigator_packet_schema():
    """The navigator's packet should be organized by the
    redline package's proposed_redlines, with a
    current_text_match field (exact / fuzzy / not_found)."""
    prompt = load_prompt("redline_apply", "navigator")
    assert "current_text_match" in prompt or "current text match" in prompt.lower()
    assert "exact" in prompt.lower()
    assert "fuzzy" in prompt.lower()
    assert "not_found" in prompt.lower() or "not found" in prompt.lower()


def test_redline_apply_navigator_grammar_notes():
    """The navigator should extract grammar notes (voice, tense,
    defined terms) from the surrounding text so the
    applicator can verify proposed_text matches."""
    prompt = load_prompt("redline_apply", "navigator")
    assert "grammar_notes" in prompt or "grammar notes" in prompt.lower()


def test_redline_apply_navigator_contradiction_check():
    """The navigator should check for clauses that might
    contradict the proposed_text (e.g. a cap on liability
    contradicting a 'no cap' clause)."""
    prompt = load_prompt("redline_apply", "navigator")
    assert "contradict" in prompt.lower() or "contradiction" in prompt.lower()


def test_redline_apply_navigator_adjacent_text_to_preserve():
    """The navigator should extract text immediately before
    and after each redline so the applicator doesn't
    accidentally modify it."""
    prompt = load_prompt("redline_apply", "navigator")
    assert "adjacent" in prompt.lower() or "preserve" in prompt.lower()


def test_redline_apply_prompts_distinct_from_redline_suggest():
    """The two redline tasks (suggest and apply) should have
    different content. Suggest proposes; apply transforms."""
    suggest = load_prompt("redline_suggest", "reviewer")
    apply = load_prompt("redline_apply", "reviewer")
    assert suggest != apply
    # Suggest produces a redline package
    assert "proposed_redlines" in suggest
    # Apply takes a redline package as input
    assert "redline_package" in apply.lower() or "redline package" in apply.lower()


def test_redline_apply_all_7_tasks_distinct():
    """The 7 task prompts should all be different content."""
    prompts = {}
    for task in ("dpo", "metadata", "redline_suggest",
                 "redline_apply", "clause_classification",
                 "summarize", "risk_score"):
        prompts[task] = load_prompt(task, "reviewer")
    assert len(set(prompts.values())) == 7


def test_redline_apply_reviewer_must_read_whole_contract():
    """Unlike the other tasks (which can use chunk-based
    navigation), redline_apply must verify current_text in
    the source for every redline. The prompt should teach
    this — read the whole contract if small, or chunk-read
    if large."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "whole contract" in prompt.lower() or "entire contract" in prompt.lower() or \
           "must verify" in prompt.lower()


def test_redline_apply_reviewer_no_redlines_of_redlines():
    """The agent must not propose its own redlines; it only
    applies what's in the package. New observations go in
    suggested_additional_redlines, not in the redlined
    document."""
    prompt = load_prompt("redline_apply", "reviewer")
    assert "propose" in prompt.lower()
    assert "package" in prompt.lower()
    # The phrase "not in redline package" should appear
    assert "not in" in prompt.lower() or "don't propose" in prompt.lower()
