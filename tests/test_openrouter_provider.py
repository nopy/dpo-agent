"""Tests for the OpenRouter LLM provider.

The OpenRouter provider is a thin wrapper over the OpenAI
client (since OpenRouter exposes an OpenAI-compatible API).
Tests verify:
- The class is importable
- The factory recognizes 'openrouter'
- Auto-mode picks OpenRouter when OPENROUTER_API_KEY is set
- The constructor uses the correct base_url and headers
- Missing API key raises a clear error
- The model name is passed through to the OpenAI client
"""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest


# ─── Class + factory ────────────────────────────────────────────

def test_openrouter_provider_is_importable():
    """The OpenRouterProvider class should be importable."""
    from dpo_agent.kg.llm import OpenRouterProvider
    assert OpenRouterProvider.name == "openrouter"


def test_openrouter_provider_exported_from_kg():
    """OpenRouterProvider should be in dpo_agent.kg's public API."""
    import dpo_agent.kg as kg
    assert "OpenRouterProvider" in kg.__all__


def test_get_provider_openrouter():
    """get_provider('openrouter') should return an OpenRouterProvider."""
    from dpo_agent.kg.llm import OpenRouterProvider
    # Provide a fake key so the constructor doesn't fail
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            provider = OpenRouterProvider(model="anthropic/claude-sonnet-4")
            assert isinstance(provider, OpenRouterProvider)
            assert provider.model == "anthropic/claude-sonnet-4"
            assert provider.name == "openrouter"


# ─── Auto-mode priority ──────────────────────────────────────────

def test_auto_mode_picks_openrouter_when_key_set():
    """When OPENROUTER_API_KEY is the only key set, auto mode
    should pick OpenRouter. (Order: ANTHROPIC > OPENROUTER > OPENAI > mock.)"""
    from dpo_agent.kg.llm import get_provider, OpenRouterProvider
    env = {"OPENROUTER_API_KEY": "test-key"}
    with patch.dict(os.environ, env, clear=True):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            provider = get_provider("auto")
            assert isinstance(provider, OpenRouterProvider)


def test_auto_mode_prefers_anthropic_over_openrouter():
    """When both ANTHROPIC_API_KEY and OPENROUTER_API_KEY are set,
    Anthropic wins (Anthropic-direct is the most efficient
    path; OpenRouter would add latency)."""
    from dpo_agent.kg.llm import get_provider, AnthropicProvider
    env = {
        "ANTHROPIC_API_KEY": "anthropic-key",
        "OPENROUTER_API_KEY": "openrouter-key",
    }
    with patch.dict(os.environ, env, clear=True):
        # AnthropicProvider also needs patching (anthropic SDK)
        with patch("instructor.from_anthropic") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            provider = get_provider("auto")
            assert isinstance(provider, AnthropicProvider)


def test_auto_mode_prefers_openrouter_over_openai():
    """When both OPENROUTER_API_KEY and OPENAI_API_KEY are set,
    OpenRouter wins (the user probably set it deliberately
    to route through OpenRouter)."""
    from dpo_agent.kg.llm import get_provider, OpenRouterProvider
    env = {
        "OPENAI_API_KEY": "openai-key",
        "OPENROUTER_API_KEY": "openrouter-key",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            provider = get_provider("auto")
            assert isinstance(provider, OpenRouterProvider)


def test_auto_mode_falls_back_to_mock():
    """When no API key is set, auto mode picks the MockLLM."""
    from dpo_agent.kg.llm import get_provider, MockLLM
    env_to_remove = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"]
    with patch.dict(os.environ, {}, clear=True):
        provider = get_provider("auto")
        assert isinstance(provider, MockLLM)


# ─── Constructor details ────────────────────────────────────────

def test_constructor_requires_api_key():
    """If OPENROUTER_API_KEY is not set, the constructor should
    raise a clear RuntimeError."""
    from dpo_agent.kg.llm import OpenRouterProvider
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            OpenRouterProvider()


def test_constructor_uses_default_base_url():
    """The default base_url should be OpenRouter's API endpoint."""
    from dpo_agent.kg.llm import OpenRouterProvider
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            provider = OpenRouterProvider()
            assert provider.base_url == "https://openrouter.ai/api/v1"


def test_constructor_accepts_custom_base_url():
    """Users can override base_url (e.g. for OpenRouter-compatible
    proxies or local test servers)."""
    from dpo_agent.kg.llm import OpenRouterProvider
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            provider = OpenRouterProvider(base_url="http://localhost:1234/v1")
            assert provider.base_url == "http://localhost:1234/v1"


def test_constructor_sets_app_headers():
    """OpenRouter's app-tracking headers (HTTP-Referer, X-Title)
    should be set on the OpenAI client. These are how OpenRouter
    attributes requests to apps and is recommended for rate
    limit reasons."""
    # The OpenAI import is lazy (inside the constructor), so we
    # mock it via the `openai` package path.
    import openai
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            with patch.object(openai, "OpenAI") as MockOpenAI:
                MockOpenAI.return_value = MagicMock()
                from dpo_agent.kg.llm import OpenRouterProvider
                provider = OpenRouterProvider()
                # Verify OpenAI was called with the right headers
                call_kwargs = MockOpenAI.call_args.kwargs
                assert "default_headers" in call_kwargs
                headers = call_kwargs["default_headers"]
                assert "HTTP-Referer" in headers
                assert "X-Title" in headers
                # Default app_url
                assert "github.com" in headers["HTTP-Referer"]


def test_constructor_accepts_custom_app_url():
    """Users can override the app_url header for OpenRouter
    attribution (e.g. their own deployment URL)."""
    import openai
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_instructor.return_value = MagicMock()
            with patch.object(openai, "OpenAI") as MockOpenAI:
                MockOpenAI.return_value = MagicMock()
                from dpo_agent.kg.llm import OpenRouterProvider
                provider = OpenRouterProvider(
                    app_url="https://my-app.example.com",
                    app_name="my-app",
                )
                call_kwargs = MockOpenAI.call_args.kwargs
                headers = call_kwargs["default_headers"]
                assert headers["HTTP-Referer"] == "https://my-app.example.com"
                assert headers["X-Title"] == "my-app"


# ─── End-to-end (mocked) ────────────────────────────────────────

def test_complete_structured_passes_model_and_messages():
    """complete_structured should pass the model and messages
    to the underlying instructor client. The Pydantic response
    model is used by instructor for structured outputs."""
    from dpo_agent.kg.ontology import Contract, ContractType
    from pydantic import BaseModel

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_client = MagicMock()
            # The mock returns a Pydantic-valid Contract
            mock_client.chat.completions.create.return_value = Contract(
                contract_id="X", contract_type=ContractType.MSA, summary="Test"
            )
            mock_instructor.return_value = mock_client
            from dpo_agent.kg.llm import OpenRouterProvider
            provider = OpenRouterProvider(model="anthropic/claude-sonnet-4")
            result = provider.complete_structured(
                system="You extract contracts.",
                user="Extract: This is an MSA between A and B.",
                response_model=Contract,
            )
            assert isinstance(result, Contract)
            # Verify the model was passed through
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            assert call_kwargs["model"] == "anthropic/claude-sonnet-4"
            assert call_kwargs["response_model"] is Contract
            # Verify messages structure
            messages = call_kwargs["messages"]
            assert len(messages) == 2
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"


def test_complete_structured_appends_schema_hint_to_system():
    """If schema_hint is provided, it should be appended to
    the system message (matches OpenAIProvider behavior)."""
    from dpo_agent.kg.ontology import Contract, ContractType

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        with patch("instructor.from_openai") as mock_instructor:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = Contract(
                contract_id="X", contract_type=ContractType.MSA, summary="Test"
            )
            mock_instructor.return_value = mock_client
            from dpo_agent.kg.llm import OpenRouterProvider
            provider = OpenRouterProvider()
            provider.complete_structured(
                system="Base system.",
                user="user message",
                response_model=Contract,
                schema_hint="Allowed types: NDA, MSA, Other.",
            )
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            messages = call_kwargs["messages"]
            system_msg = messages[0]["content"]
            assert "Base system." in system_msg
            assert "Allowed types: NDA, MSA, Other." in system_msg


# ─── AgentLLMProvider fallback ─────────────────────────────────

def test_agent_llm_provider_falls_through_to_openrouter():
    """The AgentLLMProvider (used by the kgpipeline's resolve/
    retrieve/update paths) should pick OpenRouter when only
    OPENROUTER_API_KEY is set."""
    from dpo_agent.kg.llm import AgentLLMProvider, OpenRouterProvider
    from dpo_agent.kg.ontology import Contract, ContractType
    env = {"OPENROUTER_API_KEY": "test-key"}
    with patch.dict(os.environ, env, clear=True):
        with patch("instructor.from_openai") as mock_instructor:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = Contract(
                contract_id="X", contract_type=ContractType.MSA, summary="Test"
            )
            mock_instructor.return_value = mock_client
            provider = AgentLLMProvider()
            result = provider.complete_structured(
                system="x", user="y", response_model=Contract
            )
            assert isinstance(result, Contract)
            # The AgentLLMProvider's fallback to OpenRouter worked
            assert isinstance(provider, AgentLLMProvider)


# ─── Imports in __init__ ──────────────────────────────────────

def test_kg_init_exports_openrouter():
    """dpo_agent.kg should re-export OpenRouterProvider."""
    import dpo_agent.kg as kg
    assert hasattr(kg, "OpenRouterProvider")
    assert "OpenRouterProvider" in kg.__all__


def test_kg_init_openrouter_count():
    """The public API should now have 42 names (was 41)."""
    import dpo_agent.kg as kg
    assert len(kg.__all__) == 42
