"""Tests for the obligations task.

The obligations task is the eighth built-in task. It reads a
contract and produces a structured list of binding obligations
using the 5-field wiki schema (obligor / obligee / action /
deadline / condition) plus 4 optional fields (severity,
recurring, monetary_amount, currency).

Key differences from other tasks:
- Granularity: ONE row per binding commitment, NOT per
  clause. A single "Indemnification" clause typically
  produces 2-3 obligations.
- The output has a 12-category obligation_type taxonomy
  (payment, delivery, confidentiality, etc.) — smaller than
  clause_classification's 12+ category CUAD-style taxonomy.
- The output is a JSON object with `obligations` array
  (not a flat list) so the calling code can include an
  executive_summary rollup.
- The discipline explicitly forbids extracting boilerplate
  (governing law, severability, entire agreement) and
  disclaimers as obligations.
"""

from __future__ import annotations

import pytest

from dpo_agent.tasks.loader import load_prompt, list_tasks


def test_obligations_task_is_listed():
    tasks = list_tasks()
    assert "obligations" in tasks
    assert len(tasks) >= 8  # was 7, may grow


def test_obligations_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("obligations", prompt_type)
        assert len(prompt) > 500


def test_obligations_reviewer_5_field_schema():
    """The reviewer should produce the canonical 5-field
    obligation schema from the wiki: obligor, obligee, action,
    deadline, condition."""
    prompt = load_prompt("obligations", "reviewer")
    for field in ("obligor", "obligee", "action", "deadline", "condition"):
        assert field in prompt.lower(), f"missing {field}"


def test_obligations_reviewer_4_optional_fields():
    """The reviewer should also have 4 optional fields:
    severity, recurring, monetary_amount, currency."""
    prompt = load_prompt("obligations", "reviewer")
    for field in ("severity", "recurring", "monetary_amount", "currency"):
        assert field in prompt.lower(), f"missing {field}"


def test_obligations_reviewer_12_type_taxonomy():
    """The reviewer should define a 12-category obligation
    type taxonomy."""
    prompt = load_prompt("obligations", "reviewer")
    types = ["payment", "delivery", "confidentiality", "indemnification",
             "warranty", "compliance", "notification", "cooperation",
             "restriction", "renewal", "termination", "other"]
    found = sum(1 for t in types if t in prompt.lower())
    assert found >= 11  # at least 11 of 12 types mentioned


def test_obligations_reviewer_output_3_blocks():
    """The reviewer should produce 3 blocks:
    executive_summary, obligations, open_questions."""
    prompt = load_prompt("obligations", "reviewer")
    assert "executive_summary" in prompt
    assert "obligations" in prompt
    assert "open_questions" in prompt


def test_obligations_reviewer_never_invent():
    """The prompt must explicitly forbid inventing obligor,
    obligee, action, or deadline. If unclear, leave null
    and surface in open_questions."""
    prompt = load_prompt("obligations", "reviewer")
    assert "never invent" in prompt.lower() or "do not invent" in prompt.lower()


def test_obligations_reviewer_one_row_per_commitment():
    """The defining discipline: ONE row per binding commitment,
    NOT per clause. A single clause can have multiple
    obligations."""
    prompt = load_prompt("obligations", "reviewer")
    lower = prompt.lower()
    # The phrase "one row per" or "decompose" should appear
    assert "one row per" in lower or "decompose" in lower
    # Specifically: a single clause can impose multiple obligations
    assert "multiple" in lower
    assert "binding commitment" in lower or "binding" in lower


def test_obligations_reviewer_boilerplate_filter():
    """The prompt must explicitly teach: don't extract
    boilerplate (governing law, severability, entire
    agreement, notices, signatures, definitions) as
    obligations."""
    prompt = load_prompt("obligations", "reviewer")
    lower = prompt.lower()
    assert "boilerplate" in lower
    assert "governing law" in lower
    assert "severability" in lower or "entire agreement" in lower


def test_obligations_reviewer_no_disclaimers():
    """Disclaimers ('Provider disclaims all warranties') are
    the opposite of obligations. The prompt must teach
    this."""
    prompt = load_prompt("obligations", "reviewer")
    assert "disclaim" in prompt.lower() or "warranty" in prompt.lower()


def test_obligations_reviewer_verbatim_discipline():
    """Every obligation's verbatim_text must be an exact quote
    from the source. The downstream consumer uses this to
    verify."""
    prompt = load_prompt("obligations", "reviewer")
    assert "verbatim" in prompt.lower()


def test_obligations_reviewer_defined_terms():
    """The prompt should teach: use the contract's defined
    terms ('Provider', 'Customer') not the full names
    ('Acme Corp') in obligor/obligee fields, unless the
    contract itself uses the full names."""
    prompt = load_prompt("obligations", "reviewer")
    assert "defined term" in prompt.lower() or "defined_term" in prompt.lower()


def test_obligations_reviewer_severity_levels():
    """The reviewer should define the 4 severity levels:
    critical, high, medium, low."""
    prompt = load_prompt("obligations", "reviewer")
    for level in ("critical", "high", "medium", "low"):
        assert level in prompt.lower(), f"missing severity {level}"


def test_obligations_reviewer_confidence_levels():
    """The reviewer should define 3 confidence levels:
    high, medium, low."""
    prompt = load_prompt("obligations", "reviewer")
    for level in ("high", "medium", "low"):
        assert level in prompt.lower(), f"missing confidence {level}"


def test_obligations_reviewer_preserve_null():
    """If a field is unclear, set it to null and surface in
    open_questions. Don't guess."""
    prompt = load_prompt("obligations", "reviewer")
    assert "null" in prompt.lower()


def test_obligations_critique_5_axes():
    """The critique prompt should cover the 5 axes:
    1. Grounding of verbatim_text
    2. Decomposition completeness
    3. Boilerplate filter
    4. Confidence calibration
    5. Severity calibration
    """
    prompt = load_prompt("obligations", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "decomposition" in lower or "decompose" in lower
    assert "boilerplate" in lower
    assert "confidence" in lower
    assert "severity" in lower


def test_obligations_critique_decomposition_check():
    """The critique prompt should specifically check that
    the first pass didn't combine multiple obligations into
    one row. This is the #1 failure mode."""
    prompt = load_prompt("obligations", "critique")
    assert "decomposition" in prompt.lower() or "decompose" in prompt.lower()


def test_obligations_critique_boilerplate_filter_check():
    """The critique prompt should remove over-included
    boilerplate rows in the first pass."""
    prompt = load_prompt("obligations", "critique")
    assert "boilerplate" in prompt.lower()
    assert "filter" in prompt.lower() or "remove" in prompt.lower()


def test_obligations_navigator_packet_schema():
    """The navigator's packet should be organized by
    obligation type (12 categories), with verbatim excerpts
    and a multi_obligation_clauses field for clauses that
    impose multiple obligations."""
    prompt = load_prompt("obligations", "navigator")
    assert "obligation_type" in prompt or "obligation type" in prompt.lower()
    assert "multi_obligation_clauses" in prompt or "multi obligation" in prompt.lower() or \
           "multiple obligation" in prompt.lower()


def test_obligations_navigator_groups_by_section():
    """The navigator should group reads by section to minimize
    chunk reads (similar to other tasks)."""
    prompt = load_prompt("obligations", "navigator")
    assert "group" in prompt.lower()


def test_obligations_navigator_boilerplate_separate():
    """The navigator should have a separate section for
    skipped clauses (boilerplate, definitions, disclaimers)
    so the detector knows what was filtered out."""
    prompt = load_prompt("obligations", "navigator")
    assert "skipped" in prompt.lower() or "boilerplate" in prompt.lower()
    assert "reason_skipped" in prompt or "reason skipped" in prompt.lower() or \
           "skipped" in prompt.lower()


def test_obligations_prompts_distinct_from_others():
    """The 8 task prompts should all be different content."""
    prompts = {}
    for task in ("dpo", "metadata", "redline_suggest", "redline_apply",
                 "clause_classification", "summarize", "risk_score",
                 "obligations"):
        prompts[task] = load_prompt(task, "reviewer")
    assert len(set(prompts.values())) == 8


def test_obligations_reviewer_cite_section_and_chunk():
    """Every obligation must cite its clause_ref (section
    number) for traceability."""
    prompt = load_prompt("obligations", "reviewer")
    assert "clause_ref" in prompt or "clause ref" in prompt.lower()
    assert "section" in prompt.lower()


def test_obligations_reviewer_executive_summary_rollups():
    """The executive_summary should include rollups by
    type, severity, and obligor — useful for a CLM
    dashboard."""
    prompt = load_prompt("obligations", "reviewer")
    assert "by_type" in prompt or "by type" in prompt.lower()
    assert "by_severity" in prompt or "by severity" in prompt.lower()
    assert "by_obligor" in prompt or "by obligor" in prompt.lower()


def test_obligations_reviewer_no_aspirational_language():
    """The prompt should teach: don't extract aspirational
    language ('the parties intend to ...') as obligations.
    These aren't enforceable."""
    prompt = load_prompt("obligations", "reviewer")
    assert "aspirational" in prompt.lower() or "intend" in prompt.lower()
