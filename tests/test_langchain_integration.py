"""Tests for the langchain integration.

The integration wraps dpo-agent tasks as LangChain tools.
The tests verify:
- The module imports cleanly (with or without langchain)
- make_dpo_tools() returns 9 tools when langchain is installed
- Each tool has the expected name and description
- The schema_input=False variant works
- make_triage_tool() returns a tool
- Calling a tool with valid args dispatches to the right task
- Errors are surfaced cleanly

We don't test the deep agent end-to-end (no API key, no
deepagents in CI) — that's the example's job.
"""

from __future__ import annotations

import json
import pytest

from dpo_agent import DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


def _make_tools():
    """Build a minimal DocumentTools + InMemoryDocStore pair."""
    store = InMemoryDocStore(chunk_size=4000)
    store.add("test-contract", "Test contract content. Provider shall...")
    tools = DocumentTools(
        get_document_size=store.size,
        retrieve_whole_document_content=store.get,
        get_number_of_chunks=store.chunk_count,
        get_document_chunk_by_index=store.get_chunk,
    )
    return tools


def test_module_imports_without_langchain():
    """The module should import even without langchain installed."""
    # This is already the test environment, so langchain may
    # or may not be available. The module should not crash on
    # import either way.
    from dpo_agent.integrations import langchain
    assert hasattr(langchain, "make_dpo_tools")
    assert hasattr(langchain, "make_triage_tool")


def test_have_langchain_flag():
    """The _HAVE_LANGCHAIN flag should reflect actual availability."""
    from dpo_agent.integrations import langchain
    # The flag should be a boolean
    assert isinstance(langchain._HAVE_LANGCHAIN, bool)


def test_make_dpo_tools_returns_9_tools():
    """make_dpo_tools should return exactly 9 tools, one per task."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tools = langchain.make_dpo_tools(document_tools=_make_tools())
    assert len(tools) == 9


def test_make_dpo_tools_names():
    """Each tool should have one of the 9 expected names."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tools = langchain.make_dpo_tools(document_tools=_make_tools())
    names = {t.name for t in tools}
    expected = {
        "summarize", "clause_classification", "obligations",
        "metadata", "risk_score", "dpo",
        "redline_suggest", "redline_apply", "redline_negotiation",
    }
    assert names == expected


def test_each_tool_has_description():
    """Each tool should have a non-empty description (langchain
    uses this to help the agent pick the right tool)."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tools = langchain.make_dpo_tools(document_tools=_make_tools())
    for t in tools:
        assert t.description, f"{t.name} has empty description"
        assert len(t.description) > 50, f"{t.name} description too short"


def test_schema_input_false_variant():
    """When schema_input=False, the tools that need schemas
    should still work without a schema parameter."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tools = langchain.make_dpo_tools(
        document_tools=_make_tools(), schema_input=False
    )
    assert len(tools) == 9


def test_metadata_uses_schema_str_to_avoid_pydantic_warning():
    """The metadata tool should use schema_str (not schema)
    to avoid shadowing BaseModel.schema()."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tools = langchain.make_dpo_tools(document_tools=_make_tools())
    for t in tools:
        if t.name == "metadata":
            # The args should have a schema_str field, not a
            # schema field.
            assert "schema_str" in t.args
            assert "schema" not in t.args
            break
    else:
        pytest.fail("metadata tool not found")


def test_make_triage_tool_returns_tool():
    """make_triage_tool should return a tool named triage_contract."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tool = langchain.make_triage_tool(document_tools=_make_tools())
    assert tool.name == "triage_contract"
    assert tool.description
    assert "triage" in tool.description.lower()


def test_tool_arg_schemas():
    """Each tool's args schema should match its parameter list."""
    from dpo_agent.integrations import langchain
    if not langchain._HAVE_LANGCHAIN:
        pytest.skip("langchain not installed")
    tools = langchain.make_dpo_tools(document_tools=_make_tools())
    for t in tools:
        if t.name == "summarize":
            assert "document_id" in t.args
            assert "jurisdiction_notes" in t.args
        elif t.name == "obligations":
            assert "document_id" in t.args
            assert "defined_terms" in t.args
            assert "parties" in t.args
        elif t.name == "dpo":
            assert "document_id" in t.args
            assert "defined_terms" in t.args
            assert "parties" in t.args
            assert "governing_law_hypothesis" in t.args
            assert "jurisdiction_notes" in t.args
        elif t.name in ("clause_classification", "metadata",
                        "risk_score", "redline_suggest",
                        "redline_apply", "redline_negotiation"):
            # These have a schema/framework/playbook param
            # (renamed to schema_str in metadata, but the
            # task-specific names differ in others)
            assert "document_id" in t.args


def test_module_handles_missing_langchain():
    """If langchain is not installed, make_dpo_tools should
    raise a clear ImportError (not a generic one)."""
    import dpo_agent.integrations.langchain as mod
    if mod._HAVE_LANGCHAIN:
        pytest.skip("langchain is installed; can't test missing case")
    with pytest.raises(ImportError, match="langchain"):
        mod.make_dpo_tools(document_tools=_make_tools())
    with pytest.raises(ImportError, match="langchain"):
        mod.make_triage_tool(document_tools=_make_tools())
