"""Tests for the model resolution module (dpo_agent.models).

The convention is:
    DPO_AGENT_MODEL_LOW     — cheap/fast model (navigator)
    DPO_AGENT_MODEL_MEDIUM  — default model (reviewer, kg_extract)
    DPO_AGENT_MODEL_HIGH    — strong/expensive model (critique, kg_verify)

Resolution order:
    1. DPO_AGENT_MODEL_{KIND} (kind-specific)
    2. LLM_MODEL (legacy single-model override)
    3. The hardcoded default passed by the caller

For optional model fields, use resolve_optional_model which
returns None when no env var is set.
"""

from __future__ import annotations

import os

import pytest

from dpo_agent.models import (
    ALL_KINDS,
    DEFAULT_MODELS,
    KIND_ENV_VARS,
    LEGACY_ENV_VAR,
    all_resolved_models,
    resolve_model,
    resolve_optional_model,
)


# ─── Default models ──────────────────────────────────────────

def test_default_models_have_all_three_kinds():
    """DEFAULT_MODELS should have a value for each kind."""
    for kind in ALL_KINDS:
        assert kind in DEFAULT_MODELS
        assert DEFAULT_MODELS[kind]


def test_kind_env_vars_cover_all_kinds():
    """KIND_ENV_VARS should map each kind to an env var."""
    for kind in ALL_KINDS:
        assert kind in KIND_ENV_VARS
        env_var = KIND_ENV_VARS[kind]
        assert env_var.startswith("DPO_AGENT_MODEL_")


def test_all_kinds_is_low_medium_high():
    """ALL_KINDS is the canonical 3-tier model kind set."""
    assert ALL_KINDS == ("low", "medium", "high")


# ─── resolve_model: defaults ──────────────────────────────────

def test_resolve_model_returns_default_when_no_env(monkeypatch):
    """Without any env var, resolve_model returns the caller's
    default (or DEFAULT_MODELS[kind] if no default)."""
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert resolve_model("low") == DEFAULT_MODELS["low"]
    assert resolve_model("medium") == DEFAULT_MODELS["medium"]
    assert resolve_model("high") == DEFAULT_MODELS["high"]


def test_resolve_model_caller_default_overrides(monkeypatch):
    """A caller-provided default takes precedence over DEFAULT_MODELS."""
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert resolve_model("high", default="my-custom-model") == "my-custom-model"


def test_resolve_model_rejects_unknown_kind():
    """An unknown kind should raise ValueError."""
    with pytest.raises(ValueError, match="Unknown model kind"):
        resolve_model("ultra-high")


# ─── resolve_model: kind-specific env vars ──────────────────

def test_kind_specific_env_var_overrides(monkeypatch):
    """DPO_AGENT_MODEL_HIGH should override the default for 'high'."""
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "custom-opus")
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert resolve_model("high") == "custom-opus"
    # Other kinds are unaffected
    assert resolve_model("low") == DEFAULT_MODELS["low"]
    assert resolve_model("medium") == DEFAULT_MODELS["medium"]


def test_only_high_overrides_others_unchanged(monkeypatch):
    """Setting only HIGH shouldn't affect LOW or MEDIUM."""
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "opus-1")
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert resolve_model("low") == DEFAULT_MODELS["low"]
    assert resolve_model("medium") == DEFAULT_MODELS["medium"]
    assert resolve_model("high") == "opus-1"


def test_empty_env_var_falls_through_to_default(monkeypatch):
    """An empty env var should be treated as 'not set' and fall
    through to the next resolution step."""
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    # Empty string is falsy in Python, so the resolver falls through
    assert resolve_model("high", default="my-default") == "my-default"


# ─── resolve_model: legacy LLM_MODEL ─────────────────────────

def test_legacy_llm_model_overrides_all_kinds(monkeypatch):
    """LLM_MODEL is a single-model override that applies to all kinds."""
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    # All 3 kinds resolve to LLM_MODEL
    assert resolve_model("low") == "global-model"
    assert resolve_model("medium") == "global-model"
    assert resolve_model("high") == "global-model"


def test_kind_specific_overrides_legacy(monkeypatch):
    """A kind-specific env var wins over LLM_MODEL (more specific)."""
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "specific-opus")
    # HIGH gets the specific override
    assert resolve_model("high") == "specific-opus"
    # LOW and MEDIUM fall through to LLM_MODEL
    assert resolve_model("low") == "global-model"
    assert resolve_model("medium") == "global-model"


# ─── resolve_optional_model ──────────────────────────────────

def test_resolve_optional_returns_none_when_no_env(monkeypatch):
    """Without any env var, resolve_optional_model returns None."""
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert resolve_optional_model("low") is None
    assert resolve_optional_model("medium") is None
    assert resolve_optional_model("high") is None


def test_resolve_optional_uses_kind_env_var(monkeypatch):
    """When DPO_AGENT_MODEL_HIGH is set, resolve_optional_model returns it."""
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "opus-1")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert resolve_optional_model("high") == "opus-1"


def test_resolve_optional_uses_legacy(monkeypatch):
    """When LLM_MODEL is set, resolve_optional_model returns it."""
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    assert resolve_optional_model("low") == "global-model"
    assert resolve_optional_model("high") == "global-model"


def test_resolve_optional_rejects_unknown_kind():
    """An unknown kind should raise ValueError."""
    with pytest.raises(ValueError, match="Unknown model kind"):
        resolve_optional_model("ultra-high")


# ─── all_resolved_models ─────────────────────────────────────

def test_all_resolved_models_returns_all_three(monkeypatch):
    """all_resolved_models returns a dict with all 3 kinds."""
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    result = all_resolved_models()
    assert set(result.keys()) == {"low", "medium", "high"}
    assert result["low"] == DEFAULT_MODELS["low"]
    assert result["medium"] == DEFAULT_MODELS["medium"]
    assert result["high"] == DEFAULT_MODELS["high"]


def test_all_resolved_models_uses_legacy(monkeypatch):
    """If LLM_MODEL is set, all_resolved_models returns it for all kinds."""
    monkeypatch.setenv("LLM_MODEL", "shared-model")
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    result = all_resolved_models()
    assert result == {"low": "shared-model", "medium": "shared-model", "high": "shared-model"}


def test_all_resolved_models_mixes_specific_and_legacy(monkeypatch):
    """A kind-specific env var wins for that kind, LLM_MODEL for the rest."""
    monkeypatch.setenv("LLM_MODEL", "shared-model")
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "specific-opus")
    result = all_resolved_models()
    assert result == {
        "low": "shared-model",
        "medium": "shared-model",
        "high": "specific-opus",
    }


# ─── Config integration ─────────────────────────────────────

def test_agent_config_uses_resolve_model(monkeypatch):
    """AgentConfig should pick up DPO_AGENT_MODEL_MEDIUM."""
    from dpo_agent import AgentConfig
    monkeypatch.setenv("DPO_AGENT_MODEL_MEDIUM", "sonnet-from-env")
    cfg = AgentConfig()
    assert cfg.model == "sonnet-from-env"


def test_streaming_config_uses_resolve_model(monkeypatch):
    """StreamingConfig should resolve all 3 model fields from env."""
    from dpo_agent import StreamingConfig
    monkeypatch.setenv("DPO_AGENT_MODEL_LOW", "haiku-from-env")
    monkeypatch.setenv("DPO_AGENT_MODEL_MEDIUM", "sonnet-from-env")
    monkeypatch.setenv("DPO_AGENT_MODEL_HIGH", "opus-from-env")
    cfg = StreamingConfig()
    assert cfg.navigator_model == "haiku-from-env"
    assert cfg.reviewer_model == "sonnet-from-env"
    assert cfg.critique_model == "opus-from-env"


def test_streaming_config_critique_defaults_to_none(monkeypatch):
    """StreamingConfig.critique_model should default to None when
    no env var is set (preserves the 'use reviewer_model as
    fallback' semantics for backward compat)."""
    from dpo_agent import StreamingConfig
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = StreamingConfig()
    assert cfg.critique_model is None


def test_two_pass_config_critique_defaults_to_none(monkeypatch):
    """TwoPassConfig.critique_model should default to None when
    no env var is set."""
    from dpo_agent import TwoPassConfig
    monkeypatch.delenv("DPO_AGENT_MODEL_LOW", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_MEDIUM", raising=False)
    monkeypatch.delenv("DPO_AGENT_MODEL_HIGH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = TwoPassConfig()
    assert cfg.critique_model is None
    # But reviewer_model gets the medium default
    assert cfg.reviewer_model == DEFAULT_MODELS["medium"]


# ─── Public API ─────────────────────────────────────────────

def test_resolve_model_in_dpo_agent_public_api():
    """resolve_model should be in dpo_agent's public API."""
    import dpo_agent
    assert "resolve_model" in dpo_agent.__all__
    assert "resolve_optional_model" in dpo_agent.__all__
    assert "all_resolved_models" in dpo_agent.__all__
    assert "DEFAULT_MODELS" in dpo_agent.__all__
    assert "ALL_KINDS" in dpo_agent.__all__
