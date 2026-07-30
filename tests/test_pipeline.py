"""Tests for the triage pipeline.

The pipeline wrapper runs multiple tasks in sequence against
a single contract and produces a unified triage report.

Key behaviors:
- Default plan is the 5-task triage plan (summarize →
  clause_classification → obligations → risk_score → dpo).
- Custom plan support (caller can add redline_suggest or
  remove tasks).
- Per-stage results are aggregated into a TriageReport with
  both markdown and JSON output.
- Stage failures (DPOError) halt the pipeline unless
  skip_on_error is set.
- The cost gate is a placeholder; it doesn't prompt in
  programmatic mode (auto_confirm=True bypasses it).
"""

from __future__ import annotations

import json
import pytest

from dpo_agent import (
    Agent,
    DocumentTools,
    TriagePipeline,
    TriageReport,
    PipelineConfig,
    PipelineStage,
    DEFAULT_TRIAGE_PLAN,
    triage,
)
from dpo_agent.exceptions import DPOError


def _make_tools():
    """Build a minimal DocumentTools instance for testing."""
    return DocumentTools(
        get_document_size=lambda d: 0,
        retrieve_whole_document_content=lambda d: "",
        get_number_of_chunks=lambda d: 0,
        get_document_chunk_by_index=lambda d, i: "",
    )


def test_triage_pipeline_imports():
    """All pipeline symbols should be importable from the top
    level."""
    from dpo_agent import (
        TriagePipeline,
        TriageReport,
        PipelineConfig,
        PipelineStage,
        DEFAULT_TRIAGE_PLAN,
        triage,
    )
    assert TriagePipeline is not None
    assert TriageReport is not None
    assert PipelineConfig is not None
    assert PipelineStage is not None
    assert DEFAULT_TRIAGE_PLAN is not None
    assert triage is not None


def test_default_triage_plan_has_5_tasks():
    """The default plan should be the 5-task triage plan:
    summarize, clause_classification, obligations, risk_score,
    dpo. Redline tasks are opt-in (they need a playbook)."""
    assert len(DEFAULT_TRIAGE_PLAN) == 5
    assert "summarize" in DEFAULT_TRIAGE_PLAN
    assert "clause_classification" in DEFAULT_TRIAGE_PLAN
    assert "obligations" in DEFAULT_TRIAGE_PLAN
    assert "risk_score" in DEFAULT_TRIAGE_PLAN
    assert "dpo" in DEFAULT_TRIAGE_PLAN
    # Redline is NOT in the default plan
    assert "redline_suggest" not in DEFAULT_TRIAGE_PLAN
    assert "redline_apply" not in DEFAULT_TRIAGE_PLAN


def test_triage_pipeline_constructor_validates_plan():
    """Unknown task names in the plan should raise ValueError."""
    tools = _make_tools()
    config = PipelineConfig(plan=["summarize", "nonexistent_task"])
    with pytest.raises(ValueError, match="Unknown tasks"):
        TriagePipeline(tools=tools, config=config)


def test_triage_pipeline_accepts_custom_plan():
    """Custom plans should be accepted if all tasks exist."""
    tools = _make_tools()
    config = PipelineConfig(plan=["summarize"])
    pipeline = TriagePipeline(tools=tools, config=config)
    assert pipeline.config.plan == ["summarize"]


def test_pipeline_config_defaults():
    """PipelineConfig should have sensible defaults."""
    config = PipelineConfig()
    assert config.plan == DEFAULT_TRIAGE_PLAN
    assert config.cost_threshold > 0
    assert config.auto_confirm is False
    assert config.skip_on_error is False
    assert config.on_stage_complete is None


def test_triage_convenience_function_exists():
    """The `triage()` function should be a thin wrapper around
    TriagePipeline."""
    import inspect
    sig = inspect.signature(triage)
    assert "tools" in sig.parameters
    assert "document_id" in sig.parameters
    assert "plan" in sig.parameters


def test_triage_report_has_required_fields():
    """TriageReport should have the expected fields."""
    fields = {f.name for f in TriageReport.__dataclass_fields__.values()}
    assert "document_id" in fields
    assert "stages" in fields
    assert "total_elapsed_seconds" in fields
    assert "total_cost_estimate" in fields
    assert "markdown" in fields
    assert "json" in fields


def test_pipeline_stage_has_required_fields():
    """PipelineStage should have the expected fields."""
    fields = {f.name for f in PipelineStage.__dataclass_fields__.values()}
    assert "task" in fields
    assert "result" in fields
    assert "elapsed_seconds" in fields
    assert "tool_calls" in fields
    assert "chunks_read" in fields
    assert "cost_estimate" in fields
    assert "error" in fields


def test_pipeline_stage_succeeded_property():
    """A PipelineStage's `succeeded` should be True if there's
    no error, False otherwise."""
    stage_ok = PipelineStage(task="summarize")
    assert stage_ok.succeeded

    stage_err = PipelineStage(task="summarize", error="boom")
    assert not stage_err.succeeded


def test_triage_pipeline_uses_right_kwargs_per_task():
    """The pipeline should pass the right kwargs to each task.
    E.g. playbook → redline_suggest, framework → risk_score."""
    # We don't run the full pipeline (no API key), but we can
    # inspect what kwargs would be built.
    pipeline = TriagePipeline(tools=_make_tools())
    kwargs = pipeline._build_task_kwargs(
        "summarize",
        playbook="PB",
        redline_package="RP",
        taxonomy="TX",
        framework="FW",
        schema="SC",
        defined_terms={"A": "B"},
        parties=[{"name": "X", "role": "Y"}],
        jurisdiction_notes="EU + US",
    )
    # summarize doesn't get a schema kwarg
    assert "schema" not in kwargs or kwargs.get("schema") is None
    assert kwargs["defined_terms"] == {"A": "B"}
    assert kwargs["parties"] == [{"name": "X", "role": "Y"}]
    assert kwargs["jurisdiction_notes"] == "EU + US"


def test_triage_pipeline_redline_suggest_gets_playbook():
    """redline_suggest should get the playbook as its schema
    parameter."""
    pipeline = TriagePipeline(tools=_make_tools())
    kwargs = pipeline._build_task_kwargs(
        "redline_suggest",
        playbook="PLAYBOOK_JSON",
        redline_package=None,
        taxonomy=None,
        framework=None,
        schema=None,
        defined_terms=None,
        parties=None,
        jurisdiction_notes="",
    )
    assert kwargs["schema"] == "PLAYBOOK_JSON"


def test_triage_pipeline_redline_apply_gets_redline_package():
    """redline_apply should get the redline_package as its
    schema parameter."""
    pipeline = TriagePipeline(tools=_make_tools())
    kwargs = pipeline._build_task_kwargs(
        "redline_apply",
        playbook=None,
        redline_package="REDLINE_JSON",
        taxonomy=None,
        framework=None,
        schema=None,
        defined_terms=None,
        parties=None,
        jurisdiction_notes="",
    )
    assert kwargs["schema"] == "REDLINE_JSON"


def test_triage_pipeline_risk_score_gets_framework():
    """risk_score should get the framework as its schema
    parameter."""
    pipeline = TriagePipeline(tools=_make_tools())
    kwargs = pipeline._build_task_kwargs(
        "risk_score",
        playbook=None,
        redline_package=None,
        taxonomy=None,
        framework="FRAMEWORK_JSON",
        schema=None,
        defined_terms=None,
        parties=None,
        jurisdiction_notes="",
    )
    assert kwargs["schema"] == "FRAMEWORK_JSON"


def test_triage_pipeline_clause_classification_gets_taxonomy():
    """clause_classification should get the taxonomy as its
    schema parameter."""
    pipeline = TriagePipeline(tools=_make_tools())
    kwargs = pipeline._build_task_kwargs(
        "clause_classification",
        playbook=None,
        redline_package=None,
        taxonomy="TAXONOMY_JSON",
        framework=None,
        schema=None,
        defined_terms=None,
        parties=None,
        jurisdiction_notes="",
    )
    assert kwargs["schema"] == "TAXONOMY_JSON"


def test_triage_pipeline_estimate_cost_is_positive():
    """The cost estimate should be non-negative for typical
    inputs."""
    pipeline = TriagePipeline(tools=_make_tools())
    cost = pipeline._estimate_cost(chunks_read=[1, 2, 3], elapsed_seconds=10.0)
    assert cost >= 0


def test_triage_pipeline_markdown_report_includes_header():
    """The markdown report should include the document_id and
    a Stages section."""
    # We don't run the full pipeline (no API key); we just
    # check the markdown builder.
    pipeline = TriagePipeline(tools=_make_tools())
    fake_report = {
        "document_id": "test-doc",
        "total_elapsed_seconds": 30.0,
        "total_cost_estimate": 0.5,
        "stages": [
            {"task": "summarize", "succeeded": True,
             "elapsed_seconds": 5.0, "tool_calls": 3,
             "cost_estimate": 0.05,
             "output": "TL;DR: test contract."},
        ],
    }
    md = pipeline._build_markdown_report(fake_report)
    assert "test-doc" in md
    assert "Triage Report" in md
    assert "Stages" in md
    assert "summarize" in md
    assert "TL;DR" in md


def test_triage_pipeline_json_report_includes_stages():
    """The JSON report should include per-stage results."""
    pipeline = TriagePipeline(tools=_make_tools())
    stages = [
        PipelineStage(task="summarize", tool_calls=3,
                      elapsed_seconds=5.0, chunks_read=[0, 1]),
    ]
    report = pipeline._build_json_report(
        "test-doc", stages, total_elapsed=5.0, total_cost=0.05
    )
    assert report["document_id"] == "test-doc"
    assert report["total_elapsed_seconds"] == 5.0
    assert report["total_cost_estimate"] == 0.05
    assert len(report["stages"]) == 1
    assert report["stages"][0]["task"] == "summarize"
