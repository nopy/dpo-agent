"""Tests for the AgentEvent and streaming configuration.

We don't make API calls here — we test the data classes and the
event-construction logic that the streaming wrapper uses.
"""

from __future__ import annotations

import pytest

from dpo_agent import AgentEvent, StreamingConfig


class TestAgentEvent:
    def test_minimal_event(self):
        e = AgentEvent(type="agent_start", agent="navigator")
        assert e.type == "agent_start"
        assert e.agent == "navigator"
        assert e.document_id is None
        assert e.tool_name is None

    def test_full_event(self):
        e = AgentEvent(
            type="tool_call_start",
            agent="reviewer_pass1",
            document_id="doc-1",
            tool_name="get_document_chunk_by_index",
            tool_input={"document_id": "doc-1", "index": 5},
            iteration=3,
            elapsed_ms=1234,
        )
        assert e.tool_input == {"document_id": "doc-1", "index": 5}
        assert e.iteration == 3
        assert e.elapsed_ms == 1234

    def test_event_is_picklable(self):
        """AgentEvents can be pickled for transport across processes."""
        import pickle
        e = AgentEvent(
            type="tool_call_start",
            agent="reviewer_pass1",
            document_id="doc-1",
            tool_name="get_document_chunk_by_index",
            tool_input={"document_id": "doc-1", "index": 5},
        )
        # Round-trip through pickle.
        restored = pickle.loads(pickle.dumps(e))
        assert restored.type == e.type
        assert restored.tool_input == e.tool_input


class TestStreamingConfig:
    def test_defaults(self):
        c = StreamingConfig()
        assert c.navigator_model == "claude-haiku-4-5"
        assert c.reviewer_model == "claude-sonnet-5"
        assert c.critique_model is None  # falls back to reviewer
        assert c.max_iterations == 50
        assert c.cache_ttl == "ephemeral"
        assert c.include_text_chunks is False

    def test_custom_config(self):
        c = StreamingConfig(
            navigator_model="claude-haiku-4-5",
            reviewer_model="claude-opus-5",
            critique_model="claude-opus-5",
            include_text_chunks=True,
        )
        assert c.reviewer_model == "claude-opus-5"
        assert c.critique_model == "claude-opus-5"
        assert c.include_text_chunks is True
