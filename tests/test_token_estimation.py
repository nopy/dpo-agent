"""Tests for the token estimation + context-window preflight."""

from __future__ import annotations

import pytest

from dpo_agent.exceptions import ContextWindowError
from dpo_agent.llm_client import (
    _extract_error_message,
    _is_context_window_error,
)
from dpo_agent.token_estimation import (
    DEFAULT_MAX_TOKENS,
    MODEL_CONTEXT_WINDOWS,
    PreflightResult,
    estimate_tokens,
    get_context_window,
    preflight_check,
)


# ─── estimate_tokens ──────────────────────────────────


def test_estimate_tokens_empty_string():
    assert estimate_tokens("") == 0


def test_estimate_tokens_simple_string():
    # chars/3 rate, minimum 1 token
    assert estimate_tokens("hi") == 1
    assert estimate_tokens("hello world") >= 3


def test_estimate_tokens_scales_with_size():
    one_kb = estimate_tokens("a" * 1024)
    ten_kb = estimate_tokens("a" * 10240)
    # Both should scale with size — the rule of thumb is 3 chars/token
    # (between English's 4 and structured/JSON's 2). 1024 chars / 3
    # = ~341 tokens.
    assert 300 < one_kb < 400
    assert 3000 < ten_kb < 4000
    # Should be roughly 10x.
    assert 5 < (ten_kb / one_kb) < 15


def test_estimate_tokens_works_on_large_text():
    """A 1MB document should estimate as a few hundred thousand
    tokens, NOT billions (catches a bug where someone divides
    len(text) by chars_per_token wrong)."""
    text = "x" * 1_000_000
    tokens = estimate_tokens(text)
    assert 200_000 < tokens < 400_000  # ~333K expected


def test_estimate_tokens_list_of_messages():
    """Lists of dicts (Anthropic-style messages) get summed
    correctly with per-message overhead."""
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Hi there"},
            {"type": "tool_use", "id": "x", "name": "fetch", "input": {"url": "..."}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "<html>...</html>"},
        ]},
    ]
    tokens = estimate_tokens(messages)
    assert tokens > 10


# ─── get_context_window ──────────────────────────────────


def test_get_context_window_known_models():
    # Anthropic — 200K
    assert get_context_window("claude-3-5-sonnet") == 200_000
    assert get_context_window("anthropic/claude-sonnet-4-5") == 200_000
    # OpenAI — 128K
    assert get_context_window("gpt-4o") == 128_000
    assert get_context_window("openai/gpt-4-turbo") == 128_000
    # Google — 1M
    assert get_context_window("gemini-2.5-pro") == 1_000_000
    # Mistral — 32K
    assert get_context_window("mistral-7b") == 32_000


def test_get_context_window_unknown_defaults_to_safe():
    """Unknown models fall back to the conservative default,
    not to 1M (which would silently allow over-long requests)."""
    assert get_context_window("totally-unknown-future-model-xyz") == DEFAULT_MAX_TOKENS


def test_get_context_window_substring_match():
    """Match is substring-based, so OpenRouter-style IDs like
    'anthropic/claude-3-5-sonnet' still resolve."""
    assert get_context_window("anthropic/claude-3-5-sonnet") == 200_000


# ─── preflight_check ──────────────────────────────────


def test_preflight_fits_within_window():
    """A small contract and Claude's 200K window: should fit."""
    r = preflight_check(
        model="claude-sonnet-4-5",
        system="You are a DPO agent.",
        messages=[
            {"role": "user", "content": "Sample 100-char contract text." * 2}
        ],
        max_output_tokens=4096,
    )
    assert r.fits
    assert r.estimated_tokens < 1000
    assert r.window == 200_000


def test_preflight_rejects_oversized_request():
    """A 200KB contract + Qwen-flash (32K window): should reject
    immediately with a helpful message."""
    contract = "Lorem ipsum dolor sit amet. " * 30000  # 840KB
    r = preflight_check(
        model="qwen/qwen3.7-flash",
        system="You are a DPO agent.",
        messages=[
            {"role": "user", "content": contract}
        ],
        max_output_tokens=4096,
    )
    assert not r.fits
    assert r.estimated_tokens > r.usable_input
    # Message should be actionable (suggest fix)
    assert "larger context window" in r.message.lower()


def test_preflight_reserves_output_tokens():
    """The 'usable input' budget should account for max_output_tokens
    AND the safety margin. The final value is min(window - max_output,
    window * reserve_fraction)."""
    r = preflight_check(
        model="claude-sonnet-4-5",  # 200K window
        system="x" * 1000,
        messages=[],
        max_output_tokens=10_000,
    )
    # window - max_output = 200_000 - 10_000 = 190_000
    # window * 0.8 = 160_000
    # min(190_000, 160_000) = 160_000 (safety margin wins).
    assert r.usable_input == 160_000


def test_preflight_uses_actual_window_minus_outputs_when_no_margin():
    """When the reserve_fraction is 1.0 (no safety margin), the
    usable input is window - max_output_tokens directly."""
    r = preflight_check(
        model="claude-sonnet-4-5",
        system="",
        messages=[],
        max_output_tokens=10_000,
        reserve_fraction=1.0,
    )
    assert r.usable_input == 190_000


def test_preflight_applies_safety_margin():
    """The 80% reserve fraction means a 200K window caps at
    160K usable input even without max_output_tokens."""
    r = preflight_check(
        model="claude-sonnet-4-5",
        system="",
        messages=[],
        max_output_tokens=0,
    )
    # 200K * 0.8 = 160K
    assert r.usable_input == 160_000


def test_preflight_counts_tools():
    """Tools definition tokens are counted toward the budget.
    A long tools list shouldn't be silently ignored."""
    r_with_tools = preflight_check(
        model="claude-sonnet-4-5",
        system="",
        messages=[],
        tools=[
            {"name": f"tool_{i}", "description": "x" * 100, "input_schema": {}}
            for i in range(50)
        ],
        max_output_tokens=0,
    )
    r_no_tools = preflight_check(
        model="claude-sonnet-4-5",
        system="",
        messages=[],
        max_output_tokens=0,
    )
    assert r_with_tools.estimated_tokens > r_no_tools.estimated_tokens


# ─── _is_context_window_error ──────────────────────────────────


@pytest.mark.parametrize("msg", [
    "prompt is too long: 240000 tokens > 200000 maximum",
    "Input length exceeds the maximum context length",
    "context_length_exceeded",
    "Maximum context length is 128000 tokens",
    "The request exceeds the maximum context length",
    "input tokens exceed the maximum context length",
    "request too large",
    "context window exceeded",
    "Token limit exceeded for this model",
])
def test_context_window_error_patterns_match(msg):
    assert _is_context_window_error(msg) is True


@pytest.mark.parametrize("msg", [
    "Invalid API key",
    "Rate limit exceeded",
    "Model not found",
    "Permission denied",
    "Internal server error",
    "Bad request: invalid parameter",
])
def test_non_context_window_errors_dont_match(msg):
    assert _is_context_window_error(msg) is False


# ─── ContextWindowError serialization ──────────────────────────────────


def test_context_window_error_carries_metadata():
    """The error type has structured fields for the UI."""
    exc = ContextWindowError(
        "test message",
        model="qwen/qwen3.7-flash",
        estimated_tokens=50_000,
        context_window=32_000,
        usable_input=25_600,
    )
    assert exc.model == "qwen/qwen3.7-flash"
    assert exc.estimated_tokens == 50_000
    assert exc.context_window == 32_000
    assert exc.usable_input == 25_600
    assert exc.overage == 24_400


def test_context_window_error_overage_floors_at_zero():
    """If estimated < usable, overage is 0 (not negative)."""
    exc = ContextWindowError(
        "ok",
        model="x",
        estimated_tokens=10,
        context_window=1000,
        usable_input=900,
    )
    assert exc.overage == 0


def test_context_window_error_to_dict_for_sse():
    """The to_dict() shape is what the FastAPI server puts in the
    SSE error event — the frontend UI consumes this for richer
    rendering than a bare message string."""
    exc = ContextWindowError(
        "Test message",
        model="qwen/qwen3.7-flash",
        estimated_tokens=50_000,
        context_window=32_000,
        usable_input=25_600,
    )
    d = exc.to_dict()
    assert d["error"] == "context_window_exceeded"
    assert d["model"] == "qwen/qwen3.7-flash"
    assert d["estimated_tokens"] == 50_000
    assert d["context_window"] == 32_000
    assert d["usable_input"] == 25_600
    assert d["overage_tokens"] == 24_400
    assert d["message"] == "Test message"


# ─── Integration: preflight wired into Agent._call_model ──────


def test_agent_call_model_raises_context_window_error():
    """A contract that's too large for the model's context
    raises ContextWindowError BEFORE the API call (no real
    network request made)."""
    from dpo_agent import Agent, AgentConfig, DocumentTools
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    from dpo_agent.llm_client import MockClient

    # Build a tools fixture
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-dpa", "placeholder")
    tools = DocumentTools(
        get_document_size=store.size,
        retrieve_whole_document_content=store.get,
        get_number_of_chunks=store.chunk_count,
        get_document_chunk_by_index=store.get_chunk,
    )

    agent = Agent(
        tools=tools,
        task="dpo",
        client=MockClient(),  # would error if preflight didn't catch first
        config=AgentConfig(
            model="qwen/qwen3.7-flash",  # small model
            max_tokens=4096,
        ),
    )

    huge = "x" * 200_000  # ~67K tokens, way over Qwen-flash's 32K
    messages = [
        {"role": "user", "content": "doc-id=example-dpa, " + huge},
    ]

    with pytest.raises(ContextWindowError) as exc_info:
        agent._call_model(messages)

    # The error message should mention the model so the UI
    # can suggest a fix.
    assert "qwen" in str(exc_info.value).lower() or exc_info.value.model


def test_estimate_tokens_in_exports():
    """The new functions are in dpo_agent's public API."""
    import dpo_agent
    assert hasattr(dpo_agent, "estimate_tokens")
    assert hasattr(dpo_agent, "preflight_check")
    assert hasattr(dpo_agent, "ContextWindowError")
    assert hasattr(dpo_agent, "MODEL_CONTEXT_WINDOWS")
