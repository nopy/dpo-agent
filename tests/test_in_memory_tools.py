"""Tests for the InMemoryDocStore.

The InMemoryDocStore is the reference implementation of DocumentTools.
If this breaks, every example in the package breaks.

Note: the store uses Python slice semantics (out-of-range returns
'' rather than raising). Bounds enforcement is the dispatcher's
job — see tests/test_tools.py and the dispatcher tests at the
bottom of this file.
"""

from __future__ import annotations

import pytest

from dpo_agent import DocumentTools, ToolError, dispatch
from dpo_agent.exceptions import ToolError as _ToolError  # re-export
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


@pytest.fixture
def store():
    """An in-memory store with one document split into 3 chunks."""
    s = InMemoryDocStore(chunk_size=100)
    # 250 chars => 3 chunks (100, 100, 50)
    s.add("doc-1", "A" * 100 + "B" * 100 + "C" * 50)
    return s


@pytest.fixture
def tools(store):
    return store.as_document_tools()


class TestChunking:
    def test_empty_document_has_zero_chunks(self):
        s = InMemoryDocStore(chunk_size=100)
        s.add("empty", "")
        assert s.chunk_count("empty") == 0

    def test_exact_multiple_chunks(self):
        s = InMemoryDocStore(chunk_size=100)
        s.add("doc", "A" * 300)
        assert s.chunk_count("doc") == 3
        for i in range(3):
            assert len(s.get_chunk("doc", i)) == 100

    def test_partial_last_chunk(self):
        s = InMemoryDocStore(chunk_size=100)
        s.add("doc", "A" * 250)
        assert s.chunk_count("doc") == 3
        assert len(s.get_chunk("doc", 2)) == 50

    def test_chunk_size_one(self):
        s = InMemoryDocStore(chunk_size=1)
        s.add("doc", "abc")
        assert s.chunk_count("doc") == 3
        assert s.get_chunk("doc", 0) == "a"
        assert s.get_chunk("doc", 1) == "b"
        assert s.get_chunk("doc", 2) == "c"

    def test_negative_chunk_size_raises(self):
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            InMemoryDocStore(chunk_size=0)


class TestStorage:
    def test_size(self):
        s = InMemoryDocStore()
        s.add("doc", "hello")
        assert s.size("doc") == 5

    def test_get_full_text(self):
        s = InMemoryDocStore()
        s.add("doc", "hello world")
        assert s.get("doc") == "hello world"

    def test_missing_document_raises(self):
        s = InMemoryDocStore()
        with pytest.raises(KeyError):
            s.get("nonexistent")
        with pytest.raises(KeyError):
            s.size("nonexistent")
        with pytest.raises(KeyError):
            s.chunk_count("nonexistent")

    def test_out_of_range_returns_empty_string(self):
        """The store uses Python slice semantics — out-of-range returns ''.

        Bounds enforcement happens in the dispatcher (see
        tests/test_tools.py and test_chunk_out_of_range_raises_dispatcher
        below).
        """
        s = InMemoryDocStore(chunk_size=10)
        s.add("doc", "short")
        assert s.get_chunk("doc", 5) == ""

    def test_negative_index_raises(self):
        """Negative indexes raise IndexError — the store enforces
        non-negative indexes. Out-of-range positive indexes use
        Python slice semantics (returns '')."""
        s = InMemoryDocStore(chunk_size=10)
        s.add("doc", "short")
        with pytest.raises(IndexError, match="chunk index"):
            s.get_chunk("doc", -1)

    def test_out_of_range_positive_returns_empty_string(self):
        """Out-of-range positive indexes are permissive (slice semantics).
        Bounds enforcement for the API is the dispatcher's job."""
        s = InMemoryDocStore(chunk_size=10)
        s.add("doc", "short")
        assert s.get_chunk("doc", 99) == ""

    def test_chunk_out_of_range_raises_dispatcher(self, tools):
        """The dispatcher enforces the chunk-index range."""
        from dpo_agent import dispatch
        with pytest.raises(ToolError, match="out of range"):
            dispatch(
                "get_document_chunk_by_index",
                {"document_id": "doc-1", "index": 99},
                tools,
            )

    def test_negative_chunk_index_raises_dispatcher(self, tools):
        from dpo_agent import dispatch
        with pytest.raises(ToolError, match="non-negative"):
            dispatch(
                "get_document_chunk_by_index",
                {"document_id": "doc-1", "index": -1},
                tools,
            )


class TestDocumentToolsAdapter:
    def test_as_document_tools_returns_valid_adapter(self):
        from dpo_agent import DocumentTools
        s = InMemoryDocStore(chunk_size=50)
        s.add("doc", "X" * 100)
        tools = s.as_document_tools()
        assert isinstance(tools, DocumentTools)
        # Exercise the adapter end-to-end.
        from dpo_agent import dispatch
        assert dispatch("get_document_size",
                        {"document_id": "doc"}, tools) == \
               "Document size: 100 characters (25 tokens approx.)"
        assert dispatch("get_number_of_chunks",
                        {"document_id": "doc"}, tools) == \
               "Number of chunks: 2"
        assert "X" * 50 in dispatch("get_document_chunk_by_index",
                                    {"document_id": "doc", "index": 0}, tools)
