"""Smoke test: load the package, instantiate agents, check signatures.

This is the cheapest test that catches the most regressions. It
doesn't make any API calls — it just makes sure the package is
importable and the public API has the right shape.
"""

from __future__ import annotations

import inspect

import dpo_agent
from dpo_agent import (
    Agent,
    AgentConfig,
    AgentEvent,
    AgentTwoPass,
    DocumentTools,
    Navigator,
    NavigatorResult,
    ReviewResult,
    StreamingAgent,
    StreamingConfig,
    TOOLS,
    TwoPassConfig,
    TwoPassResult,
    dispatch,
)
from dpo_agent.exceptions import (
    AgentStoppedError,
    ConfigurationError,
    DPOError,
    MaxIterationsError,
    ToolError,
)


def test_package_metadata():
    assert dpo_agent.__version__ == "0.3.0"
    # 36 names: 4 agents + 4 aliases + 1 tools dataclass + 1 dispatch +
    # 1 TOOLS list + 3 configs + 3 results + 1 event + 2 task utils +
    # 5 exceptions + 6 pipeline (TriagePipeline, TriageReport,
    # PipelineConfig, PipelineStage, DEFAULT_TRIAGE_PLAN, triage)
    # + 5 model resolution (resolve_model, resolve_optional_model,
    # all_resolved_models, DEFAULT_MODELS, ALL_KINDS).
    assert len(dpo_agent.__all__) == 36


def test_all_public_api_importable():
    """The names in __all__ should all be importable from the package."""
    for name in dpo_agent.__all__:
        assert hasattr(dpo_agent, name), f"missing export: {name}"


def test_agent_constructors_are_callable():
    """Constructors should be callable with just the required args."""
    # DocumentTools needs 4 callables; we use lambdas.
    tools = DocumentTools(
        get_document_size=lambda d: 0,
        retrieve_whole_document_content=lambda d: "",
        get_number_of_chunks=lambda d: 0,
        get_document_chunk_by_index=lambda d, i: "",
    )
    # These should all construct without error.
    Agent(tools=tools)
    AgentTwoPass(tools=tools)
    Navigator(tools=tools)
    StreamingAgent(tools=tools)
    # Backwards-compat aliases
    DPOAgent = Agent
    DPOAgent(tools=tools)
    DPOAgentTwoPass = AgentTwoPass
    DPOAgentTwoPass(tools=tools)
    DPONavigator = Navigator
    DPONavigator(tools=tools)
    DPOStreamingAgent = StreamingAgent
    DPOStreamingAgent(tools=tools)


def test_public_method_signatures():
    """The public methods should have the right signatures."""
    # The new generic Agent.run.
    sig = inspect.signature(Agent.run)
    params = list(sig.parameters)
    assert "document_id" in params
    assert "schema" in params  # task-specific context
    assert "known_metadata" in params
    # Two-pass
    sig = inspect.signature(AgentTwoPass.run)
    params = list(sig.parameters)
    assert "document_id" in params
    # Navigator
    sig = inspect.signature(Navigator.navigate)
    params = list(sig.parameters)
    assert "document_id" in params
    # Streaming
    sig = inspect.signature(StreamingAgent.review_streaming)
    params = list(sig.parameters)
    assert "document_id" in params
    assert "two_pass" in params


def test_review_result_has_required_fields():
    r = ReviewResult(review="hello", tool_calls=5, chunks_read=[0, 1, 2],
                     elapsed_seconds=1.5)
    assert r.review == "hello"
    assert r.tool_calls == 5
    assert r.chunks_read == [0, 1, 2]
    assert r.elapsed_seconds == 1.5


def test_two_pass_result_has_both_reviews():
    r = TwoPassResult(
        pass1_review="draft",
        pass2_review="revised",
        pass1_tool_calls=10,
        pass2_tool_calls=5,
        chunks_read=[0, 5, 10],
        elapsed_seconds=30.0,
    )
    assert r.pass1_review == "draft"
    assert r.pass2_review == "revised"


def test_navigator_result_has_packet():
    r = NavigatorResult(packet="the packet", tool_calls=15,
                        chunks_read=list(range(20)), elapsed_seconds=20.0)
    assert r.packet == "the packet"
    assert r.tool_calls == 15
    assert r.chunks_read == list(range(20))


def test_exception_hierarchy():
    """All our exceptions should inherit from DPOError."""
    assert issubclass(ToolError, DPOError)
    assert issubclass(MaxIterationsError, DPOError)
    assert issubclass(AgentStoppedError, DPOError)
    assert issubclass(ConfigurationError, DPOError)


def test_prompts_loaded():
    """The system prompts should be loadable and non-empty."""
    from dpo_agent.tasks.loader import load_prompt
    reviewer = load_prompt("dpo", "reviewer")
    critique = load_prompt("dpo", "critique")
    navigator = load_prompt("dpo", "navigator")
    assert "DPO" in reviewer
    assert len(reviewer) > 1000
    assert "critique" in critique.lower()
    assert len(critique) > 500
    assert "navigator" in navigator.lower() or "find" in navigator.lower()
    assert len(navigator) > 1000


def test_metadata_task_exists():
    """The metadata task ships with the package."""
    from dpo_agent.tasks.loader import load_prompt
    reviewer = load_prompt("metadata", "reviewer")
    critique = load_prompt("metadata", "critique")
    navigator = load_prompt("metadata", "navigator")
    assert "metadata" in reviewer.lower()
    assert "metadata" in navigator.lower()
    assert len(reviewer) > 1000


def test_list_tasks():
    """The loader discovers at least dpo and metadata."""
    from dpo_agent.tasks.loader import list_tasks
    tasks = list_tasks()
    assert "dpo" in tasks
    assert "metadata" in tasks


def test_backwards_compat_aliases():
    """Old class names are still importable as aliases for the new
    generic names. DPOAgent IS Agent (same class), not a wrapper."""
    from dpo_agent import Agent, DPOAgent, Navigator, DPONavigator
    from dpo_agent import AgentTwoPass, DPOAgentTwoPass
    from dpo_agent import StreamingAgent, DPOStreamingAgent
    assert DPOAgent is Agent
    assert DPONavigator is Navigator
    assert DPOAgentTwoPass is AgentTwoPass
    assert DPOStreamingAgent is StreamingAgent
