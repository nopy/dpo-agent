"""Tests for the risk_score task.

The risk_score task is the sixth built-in task. It produces a
multi-dimensional risk score (legal, financial, IP,
data_protection, operational, reputational) with a weighted
aggregate and per-dimension confidence intervals.

Key differences from other tasks:
- The output is a NUMERIC score, not prose or classification
  labels. This is the only task that produces a number.
- The schema parameter is a RISK FRAMEWORK (dimensions with
  weights and rubrics), not a JSON Schema or taxonomy.
- The output has a confidence_interval per dimension
  (e.g. "7, range 6-8") to signal when human review is
  needed.
- The discipline includes "top_wins" — risks-only framing
  misses opportunities to use the contract as a template.
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_risk_score_task_is_listed():
    tasks = list_tasks()
    assert "risk_score" in tasks
    assert len(tasks) >= 6  # was 6, may grow as tasks are added


def test_risk_score_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("risk_score", prompt_type)
        assert len(prompt) > 500


def test_risk_score_reviewer_mentions_framework():
    """The reviewer prompt should reference the framework concept
    explicitly — it's the dimension/rubric source for scoring."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "framework" in prompt.lower()
    assert "rubric" in prompt.lower()


def test_risk_score_reviewer_score_scale_1_to_10():
    """The reviewer should use a 1-10 scale (not 1-100, not
    1-5). This is the industry norm for contract risk."""
    prompt = load_prompt("risk_score", "reviewer")
    # Should mention 1-10 scale
    assert "1-10" in prompt
    # Should mention the bands
    assert "1-2" in prompt or "1 to 2" in prompt
    assert "9-10" in prompt or "9 to 10" in prompt


def test_risk_score_reviewer_5_bands():
    """The reviewer should define 5 risk bands:
    1-2 minimal, 3-4 low, 5-6 medium, 7-8 high, 9-10 critical."""
    prompt = load_prompt("risk_score", "reviewer")
    bands = ["1-2", "3-4", "5-6", "7-8", "9-10"]
    for band in bands:
        assert band in prompt, f"missing band {band}"


def test_risk_score_reviewer_multi_dimensional():
    """The reviewer should score multiple dimensions, not just
    one aggregate number. Risk is multi-dimensional."""
    prompt = load_prompt("risk_score", "reviewer")
    # Common dimensions to mention
    lower = prompt.lower()
    assert "legal" in lower
    assert "financial" in lower
    assert "ip" in lower
    assert "data_protection" in lower


def test_risk_score_reviewer_weighted_aggregate():
    """The reviewer should compute a weighted aggregate using
    the framework's weights."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "weight" in prompt.lower()
    assert "aggregate" in prompt.lower() or "weighted" in prompt.lower()


def test_risk_score_reviewer_confidence_interval():
    """The reviewer should produce a confidence interval per
    dimension (e.g. "7, range 6-8"). This signals when
    human review is needed."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "confidence_interval" in prompt or "confidence interval" in prompt.lower()


def test_risk_score_reviewer_confidence_interval_width():
    """The reviewer should define the interval widths by
    confidence level: high = +/-1, medium = +/-2, low = +/-3."""
    prompt = load_prompt("risk_score", "reviewer")
    lower = prompt.lower()
    assert "+/- 1" in lower or "+/-1" in lower
    assert "+/- 2" in lower or "+/-2" in lower
    assert "+/- 3" in lower or "+/-3" in lower


def test_risk_score_reviewer_top_risks():
    """The reviewer should produce a top_risks section with
    description, dimension, severity, and mitigation."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "top_risks" in prompt or "top risks" in prompt.lower()
    assert "mitigation" in prompt.lower()


def test_risk_score_reviewer_top_wins():
    """A risk-only framing can miss opportunities. The reviewer
    should produce a top_wins section."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "top_wins" in prompt or "top wins" in prompt.lower()


def test_risk_score_reviewer_open_questions():
    """The reviewer should produce open_questions for
    dimensions the contract is silent on."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "open_questions" in prompt or "open questions" in prompt.lower()


def test_risk_score_reviewer_score_history():
    """The reviewer should support a prior_score input and
    produce a score_history section comparing the new score
    to the prior one."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "score_history" in prompt or "score history" in prompt.lower()
    assert "prior_score" in prompt or "prior" in prompt.lower()


def test_risk_score_reviewer_cite_driving_clauses():
    """Every dimension's score must cite at least one driving
    clause. If you can't find a clause that drove the score,
    the score is too high."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "driving_clauses" in prompt or "driving clauses" in prompt.lower()


def test_risk_score_reviewer_default_unknown_to_lowest_band():
    """If a dimension isn't addressed in the contract, score
    it as the lowest band (1-2) and surface in Open Questions."""
    prompt = load_prompt("risk_score", "reviewer")
    lower = prompt.lower()
    assert "default unknown" in lower or "isn't addressed" in lower or "silent" in lower
    # Specifically: 1-2 for unknown
    assert "1-2" in prompt


def test_risk_score_reviewer_never_anchor_on_contract_framing():
    """The agent should not be influenced by the contract's
    self-description ('low-risk NDA'). The score is about the
    contract as-is, not the contract's claims."""
    prompt = load_prompt("risk_score", "reviewer")
    lower = prompt.lower()
    assert "anchor" in lower or "self-description" in lower or "marketing" in lower


def test_risk_score_reviewer_score_risk_not_negotiation_effort():
    """A 7 that would require 3 hours of negotiation to fix
    should still be a 7. The score is about the contract
    as-is, not about how easy it is to fix."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "negotiation" in prompt.lower()
    # Should explicitly say score is about as-is, not effort
    assert "as-is" in prompt.lower() or "as is" in prompt.lower()


def test_risk_score_reviewer_prefer_higher_band_when_in_doubt():
    """When in doubt, classify as the higher band — humans
    can downgrade but not easily upgrade after a skim."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "downgrade" in prompt.lower() or "higher" in prompt.lower()


def test_risk_score_critique_5_axes():
    """The critique prompt should cover the 5 axes:
    1. Grounding of driving_clauses
    2. Rubric compliance
    3. Completeness (every dimension scored)
    4. Score calibration
    5. Open questions (silence identification)
    """
    prompt = load_prompt("risk_score", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "rubric" in lower
    assert "completeness" in lower
    assert "calibration" in lower or "calibrate" in lower
    assert "open question" in lower or "open_question" in lower


def test_risk_score_critique_rubric_band_check():
    """The critique prompt should specifically check that
    every score falls within a rubric band."""
    prompt = load_prompt("risk_score", "critique")
    assert "rubric band" in prompt.lower() or "rubric bands" in prompt.lower()


def test_risk_score_critique_default_unknown_check():
    """The critique prompt should check that unknown
    dimensions are scored 1-2 (lowest band) and surfaced
    in Open questions."""
    prompt = load_prompt("risk_score", "critique")
    assert "1-2" in prompt
    assert "silent" in prompt.lower() or "open question" in prompt.lower()


def test_risk_score_navigator_packet_schema():
    """The navigator's packet should be organized by framework
    dimension, with verbatim excerpts and rubric_anchors
    (verbatim phrases that map to specific rubric bands)."""
    prompt = load_prompt("risk_score", "navigator")
    assert "rubric_anchors" in prompt or "rubric anchors" in prompt.lower()
    assert "verbatim" in prompt.lower()
    assert "dimension" in prompt.lower()


def test_risk_score_navigator_groups_by_section():
    """The navigator should group reads by section to minimize
    chunk reads (similar to redline_suggest and
    clause_classification)."""
    prompt = load_prompt("risk_score", "navigator")
    assert "group" in prompt.lower()


def test_risk_score_navigator_silence_identification():
    """The navigator should identify gaps (sub-topics the
    contract is silent on) for each dimension."""
    prompt = load_prompt("risk_score", "navigator")
    assert "gaps" in prompt.lower() or "silent" in prompt.lower()


def test_risk_score_prompts_distinct_from_others():
    """The 6 task prompts should all be different content."""
    prompts = {}
    for task in ("dpo", "metadata", "redline_suggest",
                 "clause_classification", "summarize", "risk_score"):
        prompts[task] = load_prompt(task, "reviewer")
    assert len(set(prompts.values())) == 6


def test_risk_score_reviewer_optional_counterparty_input():
    """The reviewer should accept counterparty profile as an
    optional input — a tiebreaker, not a primary signal."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "counterparty" in prompt.lower()
    # Should explicitly say counterparty is a tiebreaker
    assert "tiebreaker" in prompt.lower() or "tie-breaker" in prompt.lower()


def test_risk_score_reviewer_severity_levels_for_top_risks():
    """The top_risks severity should use the same 5 levels
    as the other tasks (critical / high / medium / low /
    info)."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "critical" in prompt.lower()
    assert "high" in prompt.lower()
    assert "medium" in prompt.lower()
    assert "low" in prompt.lower()
    assert "info" in prompt.lower()


def test_risk_score_reviewer_default_framework_when_missing():
    """If the framework is missing, the agent should use a
    default 6-dimension framework rather than fail."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "default" in prompt.lower()
    # The 6 default dimensions
    default_dims = ["legal", "financial", "ip", "data_protection",
                    "operational", "reputational"]
    mentioned = sum(1 for d in default_dims if d in prompt.lower())
    # At least 5 of 6 should be mentioned
    assert mentioned >= 5


def test_risk_score_reviewer_uses_rubric_not_gut_feel():
    """The reviewer should explicitly say: don't use gut feel,
    use the rubric bands. This is a common failure mode for
    scoring agents."""
    prompt = load_prompt("risk_score", "reviewer")
    assert "gut feel" in prompt.lower() or "rubric" in prompt.lower()
