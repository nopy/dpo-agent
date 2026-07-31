"""Resolve model names from environment variables.

Convention:
    DPO_AGENT_MODEL_LOW     — cheap/fast model (navigator)
    DPO_AGENT_MODEL_MEDIUM  — default model (reviewer, kg_extract)
    DPO_AGENT_MODEL_HIGH    — strong/expensive model (critique, kg_verify)

Resolution order (per kind):
    1. The kind-specific env var (e.g. DPO_AGENT_MODEL_HIGH)
    2. The legacy LLM_MODEL env var (one-size-fits-all override)
    3. The hardcoded default passed by the caller

This is intentionally simple: no per-task overrides, no
priority chains, no config-file loading. The caller picks
the default; the env var wins when set.

Usage:
    from dpo_agent.models import resolve_model

    # In an AgentConfig:
    model: str = resolve_model("medium", default="claude-sonnet-5")

    # In Navigator:
    model: str = resolve_model("low", default="claude-haiku-4-5")

    # In kg_resolve (LLM-confirm):
    model: str = resolve_model("high", default="claude-opus-4-5")

For OPTIONAL model fields (e.g. critique_model in
TwoPassConfig), use `resolve_optional_model()` which returns
None when no env var is set (preserves the "use the
reviewer_model as fallback" semantics).
"""

from __future__ import annotations

import os
from typing import Optional

# ─── Kind → env-var map ──────────────────────────────────────────

KIND_ENV_VARS: dict[str, str] = {
    "low": "DPO_AGENT_MODEL_LOW",
    "medium": "DPO_AGENT_MODEL_MEDIUM",
    "high": "DPO_AGENT_MODEL_HIGH",
}

# Legacy single-model env var (kept for backward compat).
LEGACY_ENV_VAR = "LLM_MODEL"

# Built-in defaults. These are the dpo-agent defaults — overridable
# per kind via the env vars above.
DEFAULT_MODELS: dict[str, str] = {
    "low": "claude-haiku-4-5",        # navigator / fast LLM-confirm
    "medium": "claude-sonnet-5",       # reviewer / kg_extract
    "high": "claude-opus-4-5",         # critique / kg_verify / kg_update
}

# All known model kinds. Useful for validation in CLI / FastAPI.
ALL_KINDS = ("low", "medium", "high")


def resolve_model(kind: str, default: Optional[str] = None) -> str:
    """Resolve the model name for a given kind.

    Args:
        kind: one of "low", "medium", "high". Determines which
            env var to look for.
        default: the hardcoded default to fall back to. If None,
            uses DEFAULT_MODELS[kind].

    Returns:
        The model name string. Never empty.

    The resolution order is:
        1. DPO_AGENT_MODEL_{KIND} (uppercase) — kind-specific override
        2. LLM_MODEL — legacy single-model override
        3. The `default` argument (or DEFAULT_MODELS[kind] if None)
    """
    if kind not in KIND_ENV_VARS:
        raise ValueError(
            f"Unknown model kind: {kind!r}. "
            f"Must be one of {ALL_KINDS}."
        )
    # 1. Kind-specific env var
    env_var = KIND_ENV_VARS[kind]
    value = os.environ.get(env_var)
    if value:
        return value
    # 2. Legacy single-model env var
    value = os.environ.get(LEGACY_ENV_VAR)
    if value:
        return value
    # 3. Hardcoded default
    if default is not None:
        return default
    return DEFAULT_MODELS[kind]


def resolve_optional_model(kind: str) -> Optional[str]:
    """Resolve an optional model name.

    Returns None if neither the kind-specific env var nor the
    legacy LLM_MODEL env var is set. This is the helper for
    fields that have a built-in fallback at the call site
    (e.g. critique_model in TwoPassConfig falls back to
    reviewer_model when None).

    Args:
        kind: one of "low", "medium", "high".

    Returns:
        The model name string, or None if no env var is set.
    """
    if kind not in KIND_ENV_VARS:
        raise ValueError(
            f"Unknown model kind: {kind!r}. "
            f"Must be one of {ALL_KINDS}."
        )
    # 1. Kind-specific env var
    env_var = KIND_ENV_VARS[kind]
    value = os.environ.get(env_var)
    if value:
        return value
    # 2. Legacy single-model env var
    value = os.environ.get(LEGACY_ENV_VAR)
    if value:
        return value
    # 3. No default — return None to signal "not set"
    return None


def all_resolved_models() -> dict[str, str]:
    """Resolve all 3 kinds. Useful for the FastAPI server's
    startup healthcheck and for the CLI's --show-config."""
    return {kind: resolve_model(kind) for kind in ALL_KINDS}
