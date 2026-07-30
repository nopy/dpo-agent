"""Tests for the redline_negotiation task.

The redline_negotiation task is the ninth built-in task. It
takes FOUR inputs (original contract, firm redlines,
counterparty counter-proposal, negotiation playbook) and
produces a position-by-position analysis with 4 possible
recommended actions (accept_counterparty / counter_with_firm
/ meet_in_middle / escalate_to_human).

Key differences from other tasks:
- FOUR inputs, not one. The agent has 3 documents to read
  (original, firm redlines, counterparty counter) plus a
  playbook.
- The output is a NEGOTIATION BRIEF, not an extraction or
  classification.
- The playbook is BINDING — the agent's recommendations must
  align with preferred_outcome, fallback_outcome, and
  walk_away.
- The output includes a "counter_proposal" — the firm's
  proposed text with meet_in_middle applied. This is what
  gets sent back to the counterparty.
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_redline_negotiation_task_is_listed():
    tasks = list_tasks()
    assert "redline_negotiation" in tasks
    assert len(tasks) >= 9


def test_redline_negotiation_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("redline_negotiation", prompt_type)
        assert len(prompt) > 500


def test_redline_negotiation_reviewer_4_inputs():
    """The reviewer should take 4 inputs:
    1. Original contract
    2. Firm redlines
    3. Counterparty counter-proposal
    4. Negotiation playbook
    """
    prompt = load_prompt("redline_negotiation", "reviewer")
    # The 4 inputs are all mentioned
    assert "original contract" in prompt.lower() or "original" in prompt.lower()
    assert "firm" in prompt.lower() and "redline" in prompt.lower()
    assert "counterparty" in prompt.lower() or "counter-proposal" in prompt.lower()
    assert "playbook" in prompt.lower()


def test_redline_negotiation_reviewer_output_6_blocks():
    """The reviewer should produce 6 blocks:
    executive_summary, disputed_clauses, acceptance_clauses,
    walk_away_risk, counter_proposal, open_questions."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    for block in ("executive_summary", "disputed_clauses",
                  "acceptance_clauses", "walk_away_risk",
                  "counter_proposal", "open_questions"):
        assert block in prompt, f"missing {block}"


def test_redline_negotiation_reviewer_4_actions():
    """The reviewer should define 4 recommended actions:
    accept_counterparty, counter_with_firm, meet_in_middle,
    escalate_to_human."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    for action in ("accept_counterparty", "counter_with_firm",
                   "meet_in_middle", "escalate_to_human"):
        assert action in prompt, f"missing action {action}"


def test_redline_negotiation_reviewer_never_invent():
    """The agent must never invent positions. Every position
    must trace to a source (original, firm redlines, or
    counter-proposal)."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "never invent" in prompt.lower() or "do not invent" in prompt.lower()


def test_redline_negotiation_reviewer_playbook_binding():
    """The playbook is BINDING — the agent's recommendations
    must align with preferred_outcome, fallback_outcome, and
    walk_away. Walk_away is a hard boundary."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    lower = prompt.lower()
    assert "binding" in lower
    assert "walk_away" in lower or "walk away" in lower or "walk-away" in lower
    # The walk-away hard boundary
    assert "hard boundary" in lower or "never recommend accepting" in lower


def test_redline_negotiation_reviewer_verbatim_discipline():
    """All 3 positions (current_text, firm_position,
    counterparty_position) must be verbatim quotes."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "verbatim" in prompt.lower()


def test_redline_negotiation_reviewer_deal_context():
    """The reviewer should accept deal_context (deal_value,
    firm_alternative, counterparty_alternative, relationship)
    and use it to calibrate recommendations."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "deal_context" in prompt or "deal context" in prompt.lower()
    # Specific deal context fields
    lower = prompt.lower()
    assert "deal_value" in lower or "deal value" in lower
    assert "batna" in lower  # best alternative to negotiated agreement
    assert "relationship" in lower


def test_redline_negotiation_reviewer_concession_pattern():
    """The agent should follow the playbook's
    concession_pattern (start with preferred, offer fallback
    if pushed back, escalate if walk-away terms)."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "concession_pattern" in prompt or "concession pattern" in prompt.lower()


def test_redline_negotiation_reviewer_meet_in_middle_discipline():
    """A meet_in_middle recommendation must propose text
    between preferred and fallback (or be the fallback)."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "between" in prompt.lower() and "fallback" in prompt.lower()


def test_redline_negotiation_reviewer_escalate_when_unsure():
    """When in doubt, the agent should escalate to human
    rather than make a unilateral decision."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "escalate_to_human" in prompt
    # The escalation discipline
    assert "in doubt" in prompt.lower() or "when in doubt" in prompt.lower()


def test_redline_negotiation_reviewer_three_documents():
    """The navigator/agent should be aware of THREE
    documents, not one. The original contract, the firm
    redlines, and the counterparty counter-proposal are
    separate documents to read."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "three documents" in prompt.lower() or "3 documents" in prompt.lower() or \
           "current_document" in prompt.lower()


def test_redline_negotiation_reviewer_position_attribution():
    """Every position must be attributed to the right side.
    Saying 'the firm asked for 2x' when the firm asked for
    1x is a critical error."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "attribut" in prompt.lower()  # attribution, attribute, attributing


def test_redline_negotiation_critique_5_axes():
    """The critique prompt should cover the 5 axes:
    1. Grounding of current_text
    2. Position attribution (firm and counterparty)
    3. Playbook compliance
    4. Completeness
    5. Calibration to deal context
    """
    prompt = load_prompt("redline_negotiation", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "attribut" in lower
    assert "playbook" in lower
    assert "completeness" in lower
    assert "deal context" in lower or "deal_context" in lower


def test_redline_negotiation_critique_position_attribution():
    """The critique must specifically check that positions
    are attributed to the right side."""
    prompt = load_prompt("redline_negotiation", "critique")
    assert "attribut" in prompt.lower()


def test_redline_negotiation_critique_playbook_compliance():
    """The critique must verify that recommended_action
    aligns with the playbook's concession_pattern."""
    prompt = load_prompt("redline_negotiation", "critique")
    assert "concession_pattern" in prompt or "concession pattern" in prompt.lower()


def test_redline_negotiation_navigator_packet_schema():
    """The navigator's packet should have a section for
    disputed clauses (3 documents × verbatim positions)
    and a section for acceptance clauses."""
    prompt = load_prompt("redline_negotiation", "navigator")
    assert "disputed" in prompt.lower()
    assert "acceptance" in prompt.lower() or "agreement" in prompt.lower()


def test_redline_negotiation_navigator_three_documents():
    """The navigator's packet should organize around 3
    documents: original, firm redlines, counterparty
    counter."""
    prompt = load_prompt("redline_negotiation", "navigator")
    lower = prompt.lower()
    assert "original" in lower
    assert "firm" in lower
    assert "counterparty" in lower or "counter" in lower


def test_redline_negotiation_navigator_per_clause_gap_analysis():
    """The navigator's packet should have a per-clause
    gap_analysis field — the negotiator uses this to
    write the rationale."""
    prompt = load_prompt("redline_negotiation", "navigator")
    assert "gap_analysis" in prompt or "gap analysis" in prompt.lower()


def test_redline_negotiation_prompts_distinct_from_redline_apply():
    """The 3 redline tasks (suggest, apply, negotiation) all
    have different content."""
    suggest = load_prompt("redline_suggest", "reviewer")
    apply = load_prompt("redline_apply", "reviewer")
    negotiate = load_prompt("redline_negotiation", "reviewer")
    assert len({suggest, apply, negotiate}) == 3


def test_redline_negotiation_all_9_tasks_distinct():
    """All 9 task prompts should be different."""
    prompts = {}
    for task in ("dpo", "metadata", "redline_suggest", "redline_apply",
                 "redline_negotiation", "clause_classification",
                 "summarize", "risk_score", "obligations"):
        prompts[task] = load_prompt(task, "reviewer")
    assert len(set(prompts.values())) == 9


def test_redline_negotiation_reviewer_no_unilateral_decisions():
    """The agent is advisory, not decisive. The human makes
    the final call."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    lower = prompt.lower()
    # The agent's role is advisory
    assert "advisory" in lower or "human negotiator" in lower or \
           "human counsel" in lower
    # The human makes the decisions
    assert "human" in lower


def test_redline_negotiation_reviewer_accept_silence_actually_means_accept():
    """The agent must understand that 'firm accepted
    original' and 'counterparty accepted original' both
    mean agreement, not silence."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    lower = prompt.lower()
    # Specifically: when neither side redlined, the clause
    # is in agreement
    assert "no redline" in lower or "no counter" in lower


def test_redline_negotiation_reviewer_escalate_when_playbook_silent():
    """If the playbook is silent on a clause type, the agent
    must escalate to human rather than guess."""
    prompt = load_prompt("redline_negotiation", "reviewer")
    assert "silent" in prompt.lower() or "doesn't cover" in prompt.lower() or \
           "doesn't have" in prompt.lower()
