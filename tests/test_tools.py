"""Tests for the tools module.

The tool dispatcher is the most safety-critical piece of the package
(it's the only thing that touches the caller's document store). The
tests here don't make any API calls — they exercise the dispatcher
with in-memory implementations.
"""

from __future__ import annotations

import pytest

from dpo_agent import DocumentTools, TOOLS, dispatch
from dpo_agent.exceptions import ToolError


@pytest.fixture
def store():
    """An in-memory store with one document split into 3 chunks."""
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    s = InMemoryDocStore(chunk_size=100)
    # 250 chars => 3 chunks (100, 100, 50)
    s.add("doc-1", "A" * 100 + "B" * 100 + "C" * 50)
    return s


@pytest.fixture
def tools(store):
    return store.as_document_tools()


class TestToolSchema:
    def test_four_tools_defined(self):
        assert len(TOOLS) == 4

    def test_tool_names(self):
        names = {t["name"] for t in TOOLS}
        assert names == {
            "get_document_size",
            "retrieve_whole_document_content",
            "get_number_of_chunks",
            "get_document_chunk_by_index",
        }

    def test_all_tools_have_input_schema(self):
        for tool in TOOLS:
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"
            assert "required" in tool["input_schema"]


class TestGetDocumentSize:
    def test_returns_size_string(self, tools):
        result = dispatch("get_document_size", {"document_id": "doc-1"}, tools)
        assert "Document size: 250 characters" in result
        assert "62 tokens" in result

    def test_missing_document_id(self, tools):
        with pytest.raises(ToolError, match="Missing required parameter"):
            dispatch("get_document_size", {}, tools)


class TestRetrieveWholeDocument:
    def test_returns_full_text(self, tools):
        result = dispatch(
            "retrieve_whole_document_content", {"document_id": "doc-1"}, tools
        )
        assert len(result) == 250
        assert result == "A" * 100 + "B" * 100 + "C" * 50

    def test_refuses_large_documents(self, tools):
        # Add a 100K-char document.
        from dpo_agent.examples.in_memory_tools import InMemoryDocStore
        big_store = InMemoryDocStore(chunk_size=100)
        big_store.add("big", "X" * 100_000)
        big_tools = big_store.as_document_tools()
        with pytest.raises(ToolError, match="above the"):
            dispatch("retrieve_whole_document_content",
                     {"document_id": "big"}, big_tools)


class TestGetNumberOfChunks:
    def test_correct_count(self, tools):
        result = dispatch("get_number_of_chunks", {"document_id": "doc-1"}, tools)
        assert result == "Number of chunks: 3"


class TestGetDocumentChunkByIndex:
    def test_first_chunk(self, tools):
        result = dispatch(
            "get_document_chunk_by_index",
            {"document_id": "doc-1", "index": 0},
            tools,
        )
        assert "[Chunk 0 of 3]" in result
        assert "A" * 100 in result

    def test_last_chunk(self, tools):
        result = dispatch(
            "get_document_chunk_by_index",
            {"document_id": "doc-1", "index": 2},
            tools,
        )
        assert "[Chunk 2 of 3]" in result
        assert "C" * 50 in result

    def test_out_of_range_index(self, tools):
        with pytest.raises(ToolError, match="out of range"):
            dispatch(
                "get_document_chunk_by_index",
                {"document_id": "doc-1", "index": 5},
                tools,
            )

    def test_negative_index(self, tools):
        with pytest.raises(ToolError, match="non-negative"):
            dispatch(
                "get_document_chunk_by_index",
                {"document_id": "doc-1", "index": -1},
                tools,
            )

    def test_missing_index(self, tools):
        with pytest.raises(ToolError, match="non-negative"):
            dispatch(
                "get_document_chunk_by_index",
                {"document_id": "doc-1"},
                tools,
            )


class TestDispatcherErrors:
    def test_unknown_tool(self, tools):
        with pytest.raises(ToolError, match="Unknown tool"):
            dispatch("not_a_real_tool", {}, tools)

    def test_underlying_exception_wrapped(self):
        """A failure in the caller's implementation is wrapped in ToolError."""

        def bad_size(doc_id):
            raise RuntimeError("database is down")

        tools = DocumentTools(
            get_document_size=bad_size,
            retrieve_whole_document_content=lambda d: "",
            get_number_of_chunks=lambda d: 0,
            get_document_chunk_by_index=lambda d, i: "",
        )
        with pytest.raises(ToolError, match="database is down"):
            dispatch("get_document_size", {"document_id": "x"}, tools)
