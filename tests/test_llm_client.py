"""Tests for the LLMClient abstraction (Path D).

Covers:
- MockClient end-to-end (create + stream)
- OpenAI-to-Anthropic request translation (the wire format converter
  that runs on every OpenAI-compat call)
- OpenAI-to-Anthropic response translation (structured + streaming)
- create_client() factory logic (env-based selection, explicit
  override, error handling for missing keys)
- Backward compat: Agent/StreamingAgent still accept
  anthropic.Anthropic directly (existing tests use this path)
- StreamingAgent end-to-end with MockClient
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from dpo_agent.llm_client import (
    AnthropicClient,
    LLMClient,
    LLMResponse,
    MockClient,
    OpenAICompatClient,
    STREAM_CONTENT_BLOCK_DELTA,
    STREAM_MESSAGE_DELTA,
    STREAM_MESSAGE_START,
    STREAM_MESSAGE_STOP,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    Usage,
    _anthropic_to_openai_request,
    _openai_to_response,
    create_client,
)
from dpo_agent.exceptions import DPOError
from dpo_agent.tools import DocumentTools


# ─── MockClient ────────────────────────────────────────


def test_mock_client_create_returns_text_response():
    """MockClient.create should return a default text response."""
    client = MockClient()
    response = client.create(
        model="claude-sonnet-4-5",
        system="You are a DPO.",
        messages=[{"role": "user", "content": "Hello"}],
    )
    assert isinstance(response, LLMResponse)
    assert response.stop_reason == "end_turn"
    assert len(response.content) == 1
    assert isinstance(response.content[0], TextBlock)
    assert response.content[0].text == "Mock response."


def test_mock_client_logs_calls():
    """MockClient should log every call so tests can inspect them."""
    client = MockClient()
    client.create(
        model="claude-sonnet-4-5",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert len(client.call_log) == 1
    assert client.call_log[0]["model"] == "claude-sonnet-4-5"
    assert client.call_log[0]["system"] == "sys"


def test_mock_client_set_response():
    """Canned responses should be popped one per call."""
    client = MockClient()
    client.set_response(LLMResponse(
        content=[TextBlock(text="Canned 1")], stop_reason="end_turn"
    ))
    client.set_response(LLMResponse(
        content=[TextBlock(text="Canned 2")], stop_reason="end_turn"
    ))
    r1 = client.create(model="x", system="s", messages=[])
    r2 = client.create(model="x", system="s", messages=[])
    assert r1.content[0].text == "Canned 1"
    assert r2.content[0].text == "Canned 2"


def test_mock_client_stream_yields_canonical_events():
    """The streaming mock should yield StreamEvent objects in the
    canonical format (message_start, content_block_delta, etc.)."""
    client = MockClient()
    client.set_response(LLMResponse(
        content=[TextBlock(text="Hello world")], stop_reason="end_turn"
    ))
    with client.stream(model="x", system="s", messages=[]) as stream:
        events = list(stream)
    types = [e.type for e in events]
    # Standard Anthropic-style envelope: start, deltas, stop.
    assert STREAM_MESSAGE_START in types
    assert STREAM_CONTENT_BLOCK_DELTA in types
    assert STREAM_MESSAGE_STOP in types


def test_mock_client_stream_get_final_message():
    """stream.get_final_message() should return the full response."""
    client = MockClient()
    client.set_response(LLMResponse(
        content=[TextBlock(text="Final")],
        stop_reason="end_turn",
        usage=Usage(input_tokens=20, output_tokens=10),
    ))
    with client.stream(model="x", system="s", messages=[]) as stream:
        for _ in stream:
            pass
        final = stream.get_final_message()
    assert isinstance(final, LLMResponse)
    assert final.content[0].text == "Final"
    assert final.usage.output_tokens == 10


# ─── OpenAI request translation ────────────────────────


def test_anthropic_to_openai_request_simple():
    """A simple text-only user message translates cleanly."""
    openai_msgs, tools = _anthropic_to_openai_request(
        system="sys",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
    )
    assert len(openai_msgs) == 2  # system + user
    assert openai_msgs[0] == {"role": "system", "content": "sys"}
    assert openai_msgs[1] == {"role": "user", "content": "hello"}
    assert tools is None


def test_anthropic_to_openai_request_tool_use():
    """An assistant message with tool_use blocks gets translated
    to OpenAI's tool_calls format."""
    openai_msgs, _ = _anthropic_to_openai_request(
        system="sys",
        messages=[{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "get_document_size",
                    "input": {},
                },
            ],
        }],
        tools=None,
    )
    asst = openai_msgs[-1]
    assert asst["role"] == "assistant"
    assert asst["content"] == "Let me check."
    assert "tool_calls" in asst
    tc = asst["tool_calls"][0]
    assert tc["id"] == "toolu_abc"
    assert tc["function"]["name"] == "get_document_size"


def test_anthropic_to_openai_request_tool_result():
    """A user message with tool_result blocks becomes OpenAI tool messages."""
    openai_msgs, _ = _anthropic_to_openai_request(
        system="sys",
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_abc",
                    "content": "100 KB",
                },
            ],
        }],
        tools=None,
    )
    # The "tool" message is appended after the system message.
    assert len(openai_msgs) == 2  # system + tool
    assert openai_msgs[0] == {"role": "system", "content": "sys"}
    assert openai_msgs[1] == {
        "role": "tool",
        "tool_call_id": "toolu_abc",
        "content": "100 KB",
    }


def test_anthropic_to_openai_request_tools():
    """Anthropic input_schema → OpenAI function.parameters."""
    _, openai_tools = _anthropic_to_openai_request(
        system="sys",
        messages=[],
        tools=[{
            "name": "my_tool",
            "description": "does stuff",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
        }],
    )
    assert openai_tools is not None
    assert len(openai_tools) == 1
    fn = openai_tools[0]["function"]
    assert fn["name"] == "my_tool"
    assert fn["description"] == "does stuff"
    assert fn["parameters"]["properties"]["x"]["type"] == "integer"


def test_anthropic_to_openai_request_cache_control_system():
    """The Anthropic list-form system (with cache_control blocks)
    gets flattened to a single OpenAI system message."""
    openai_msgs, _ = _anthropic_to_openai_request(
        system=[
            {"type": "text", "text": "part1", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "part2"},
        ],
        messages=[],
        tools=None,
    )
    assert len(openai_msgs) == 1
    assert openai_msgs[0]["role"] == "system"
    assert "part1" in openai_msgs[0]["content"]
    assert "part2" in openai_msgs[0]["content"]


# ─── OpenAI response translation ────────────────────────


def test_openai_to_response_text():
    """OpenAI text response → LLMResponse with TextBlock."""
    # Construct a fake OpenAI response object
    class FakeMessage:
        content = "Hello"
        tool_calls = None

    class FakeChoice:
        message = FakeMessage()
        finish_reason = "stop"

    class FakeUsage:
        prompt_tokens = 10
        completion_tokens = 5
        cached_tokens = 0

    class FakeResponse:
        choices = [FakeChoice()]
        model = "gpt-4o-mini"
        usage = FakeUsage()

    response = _openai_to_response(FakeResponse())
    assert response.stop_reason == "end_turn"
    assert len(response.content) == 1
    assert isinstance(response.content[0], TextBlock)
    assert response.content[0].text == "Hello"
    assert response.usage.input_tokens == 10


def test_openai_to_response_tool_calls():
    """OpenAI response with tool_calls → ToolUseBlock(s)."""
    class FakeFunction:
        name = "my_tool"
        arguments = '{"x": 42}'

    class FakeToolCall:
        id = "call_1"
        function = FakeFunction()

    class FakeMessage:
        content = ""
        tool_calls = [FakeToolCall()]

    class FakeChoice:
        message = FakeMessage()
        finish_reason = "tool_calls"

    class FakeUsage:
        prompt_tokens = 5
        completion_tokens = 3
        cached_tokens = 0

    class FakeResponse:
        choices = [FakeChoice()]
        model = "gpt-4o"
        usage = FakeUsage()

    response = _openai_to_response(FakeResponse())
    assert response.stop_reason == "tool_use"
    assert len(response.content) == 1
    assert isinstance(response.content[0], ToolUseBlock)
    assert response.content[0].name == "my_tool"
    assert response.content[0].input == {"x": 42}


def test_openai_to_response_length_truncation():
    """OpenAI finish_reason='length' maps to Anthropic 'max_tokens'."""
    class FakeMessage:
        content = "truncated"
        tool_calls = None

    class FakeChoice:
        message = FakeMessage()
        finish_reason = "length"

    class FakeResponse:
        choices = [FakeChoice()]
        model = "gpt-4o"
        usage = None

    response = _openai_to_response(FakeResponse())
    assert response.stop_reason == "max_tokens"


# ─── create_client() factory ────────────────────────────


def test_create_client_explicit_anthropic(monkeypatch):
    """AnthropicClient is returned when backend='anthropic'."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = create_client(backend="anthropic")
    assert isinstance(client, AnthropicClient)


def test_create_client_explicit_openai_compat(monkeypatch):
    """OpenAICompatClient is returned when backend='openai-compat'."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = create_client(backend="openai-compat")
    assert isinstance(client, OpenAICompatClient)


def test_create_client_explicit_mock():
    """MockClient is returned when backend='mock'."""
    client = create_client(backend="mock")
    assert isinstance(client, MockClient)


def test_create_client_auto_picks_anthropic(monkeypatch):
    """Auto-mode picks Anthropic when ANTHROPIC_API_KEY is set."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = create_client()
    assert isinstance(client, AnthropicClient)


def test_create_client_auto_picks_openai_compat_openrouter(monkeypatch):
    """Auto-mode picks openai-compat (OpenRouter) when only
    OPENROUTER_API_KEY is set, with the right base_url."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = create_client()
    assert isinstance(client, OpenAICompatClient)
    assert "openrouter.ai" in client._base_url


def test_create_client_auto_picks_mock(monkeypatch):
    """Auto-mode falls back to mock when no API keys are set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = create_client()
    assert isinstance(client, MockClient)


def test_create_client_anthropic_no_key_raises(monkeypatch):
    """AnthropicClient without ANTHROPIC_API_KEY should raise."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(DPOError, match="ANTHROPIC_API_KEY"):
        create_client(backend="anthropic")


def test_create_client_openai_no_key_raises(monkeypatch):
    """OpenAICompatClient without OPENAI/OPENROUTER key should raise."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(DPOError, match="OPENROUTER_API_KEY.*OPENAI_API_KEY"):
        create_client(backend="openai-compat")


def test_create_client_unknown_backend(monkeypatch):
    """Unknown backend name should raise DPOError."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(DPOError, match="Unknown LLM backend"):
        create_client(backend="huggingface")


# ─── Backward compat (Agent still accepts anthropic.Anthropic) ─


def _make_test_tools(text: str = "Tiny contract.") -> DocumentTools:
    """Helper to build DocumentTools from a single inline text."""
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-dpa", text)
    return DocumentTools(
        get_document_size=store.size,
        retrieve_whole_document_content=store.get,
        get_number_of_chunks=store.chunk_count,
        get_document_chunk_by_index=store.get_chunk,
    )


def test_agent_accepts_anthropic_client_directly():
    """Agent should accept an AnthropicClient directly without
    going through the factory."""
    from dpo_agent.agent import Agent
    tools = _make_test_tools()
    client = MockClient()
    # Should not raise — MockClient is a valid LLMClient.
    agent = Agent(tools=tools, task="dpo", client=client)
    assert agent.client is client


def test_agent_accepts_legacy_anthropic_instance():
    """Agent should still accept a (mock) anthropic.Anthropic
    instance via the _wrap_anthropic_client shim. This is the
    backward-compat path used by existing tests."""
    from unittest.mock import MagicMock
    from dpo_agent.agent import Agent, _wrap_anthropic_client
    tools = _make_test_tools()

    # Construct a mock that looks like anthropic.Anthropic.
    fake_anthropic = MagicMock()
    # Set the class to a fake "Anthropic" type so isinstance
    # check by class name in _normalize_client fires.
    fake_anthropic.__class__ = type("Anthropic", (), {"__module__": "anthropic"})

    # Wrap manually — must succeed.
    wrapped = _wrap_anthropic_client(fake_anthropic)
    assert wrapped is not None
    assert wrapped._client is fake_anthropic

    # Pass through Agent's constructor — should also work and
    # produce an LLMClient.
    agent = Agent(tools=tools, task="dpo", client=fake_anthropic)
    from dpo_agent.llm_client import LLMClient
    assert isinstance(agent.client, LLMClient)


# ─── Streaming end-to-end with Mock ────────────────────


def test_streaming_agent_with_mock():
    """The StreamingAgent should work end-to-end with MockClient."""
    from dpo_agent.streaming import StreamingAgent, StreamingConfig
    tools = _make_test_tools("A different contract.")

    # Set up canned responses: navigator (single end_turn)
    client = MockClient()
    client.set_response(LLMResponse(
        content=[TextBlock(text="Findings: nothing.")],
        stop_reason="end_turn",
    ))
    # Then reviewer (single end_turn)
    client.set_response(LLMResponse(
        content=[TextBlock(text="## Risk score\nMedium.")],
        stop_reason="end_turn",
    ))

    agent = StreamingAgent(
        tools=tools, task="dpo",
        client=client,
        config=StreamingConfig(task="dpo", include_text_chunks=False),
    )
    events = list(agent.review_streaming(
        document_id="example-dpa", two_pass=False,  # skip pass 2 for simplicity
    ))
    # Should have at least an agent_start for each stage and an agent_complete.
    types = [e.type for e in events]
    assert "agent_start" in types
    assert "agent_complete" in types
