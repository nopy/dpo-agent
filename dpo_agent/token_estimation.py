"""Token estimation + context-window preflight.

The dpo-agent's Agent / Navigator / StreamingAgent classes
call an LLM with a system prompt + a growing list of
messages (tool calls accumulate over the loop's iterations).
If the request exceeds the model's context window, the
provider returns a 400 with a generic message like
"prompt is too long" or "context length exceeded".

To avoid:
  - Paying for a 60-second API call that will fail
  - Showing the user a generic error message
  - Being unable to recommend a fix

we estimate the request size client-side and reject
over-limit requests BEFORE the API call. Estimates use
chars/4 for English (the OpenAI rule of thumb) and
chars/2 for structured / non-whitespace-heavy text.
We treat the request as ~75% English / 25% structured
when we don't know, which is a safe over-estimate and
better to be conservative (reject too-small) than to let
the call through and fail server-side.

# Model context windows

This module keeps a hard-coded table of common model
context windows. When the selected model isn't in the
table, we fall back to a conservative "small model" cap
(8K tokens). The table is consulted by name match
(case-insensitive substring), so "anthropic/claude-3-5-sonnet"
resolves to the Claude row via the "claude" / "sonnet" tokens.

This is best-effort. The authoritative source is the model
provider, and the exact window depends on the model's
deployment config. We err on the safe side (cap at 8K for
unknown models) so unknown models can't silently exceed
their window.

# Adding new models

To add a model's window to the table, append to
MODEL_CONTEXT_WINDOWS. Format:
    "<substring>": <tokens>

Match is case-insensitive substring. The first matching
substring wins, so order matters (most specific first).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional


# Default cap for unknown models. Conservative — most
# frontier models have 8K minimum; flashing/quantized
# variants often have less. The preflight rejects anything
# over this, but the API might still accept it; that's
# fine — this is just a defensive default.
DEFAULT_MAX_TOKENS = 8192

# Reserve a fraction of the model's window for the model's
# response (max_tokens) and overhead (system, tool defs).
# 80% leaves 20% for output, which is enough for most review
# outputs (<=4K tokens). For tasks with longer expected
# outputs, increase RESERVE_FRACTION.
RESERVE_FRACTION = 0.8


# Approximate tokens-per-character. English averages ~4 chars
# per token; structured / code-heavy text averages ~2 chars
# per token. For mixed content (the dpo-agent case), use a
# weighted blend that biases toward the more restrictive
# (2 chars/token) so the estimate is conservative.
ENGLISH_CHARS_PER_TOKEN = 4
STRUCTURED_CHARS_PER_TOKEN = 2


# ─── Public API ──────────────────────────────────────────


def estimate_tokens(content: str | list[str | dict]) -> int:
    """Estimate token count for a string or list of strings / messages.

    Strings are estimated as 1 token per 2 characters (the
    conservative rate, which covers English + structured
    content).

    For messages (list of dicts with role + content), we sum
    per-message estimates plus a small per-message overhead
    for the role label and JSON separators (~4 tokens each).

    This is intentionally conservative — over-estimating
    causes us to reject borderline-but-OK requests; under-
    estimating causes API errors. The cost of an over-
    estimate is "user has to chunk their contract"; the cost
    of an under-estimate is "user pays for an API call that
    returns a 400".
    """
    if isinstance(content, str):
        return _estimate_string(content)
    if isinstance(content, list):
        # Could be a list of strings OR a list of message dicts.
        total = 0
        for item in content:
            if isinstance(item, str):
                total += _estimate_string(item)
            elif isinstance(item, dict):
                # Anthropic-style message block. Estimate per
                # block, plus per-message overhead.
                if item.get("type") == "text":
                    total += _estimate_string(item.get("text", ""))
                elif item.get("type") in ("tool_use", "tool_result"):
                    # Tool blocks have structured content; more
                    # tokens per char.
                    serialized = str(item.get("input") or item.get("content") or "")
                    total += _estimate_string(serialized, structured=True)
                    total += 4  # tool name / id overhead
                else:
                    # Unknown block — fall back to the dict's
                    # JSON representation.
                    total += _estimate_string(str(item), structured=True)
                total += 4  # message header overhead
            else:
                total += _estimate_string(str(item))
        return total
    return _estimate_string(str(content))


def get_context_window(
    model: str,
    table: Optional[dict[str, int]] = None,
) -> int:
    """Look up the context window for `model` (in tokens).

    Uses substring match against MODEL_CONTEXT_WINDOWS by
    default. Returns DEFAULT_MAX_TOKENS if no match found.

    Substring matching is case-insensitive. The first
    matching entry wins.
    """
    table = table or MODEL_CONTEXT_WINDOWS
    model_lc = model.lower()
    for substring, window in table.items():
        if substring.lower() in model_lc:
            return window
    return DEFAULT_MAX_TOKENS


def preflight_check(
    model: str,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    *,
    max_output_tokens: int = 4096,
    reserve_fraction: float = RESERVE_FRACTION,
) -> "PreflightResult":
    """Run a preflight token budget check before the API call.

    Returns a `PreflightResult` with the estimated input
    token count, the model's available input budget, and
    whether the request fits.

    The available input budget is:
        context_window - max_output_tokens
    capped to reserve_fraction * context_window as a safety
    margin (in case the model's real window is smaller than
    what we have on file).

    If the estimate exceeds the budget, returns `fits=False`
    with a `message` describing the issue. Caller should
    refuse to send the request and surface the message.
    """
    system_tokens = estimate_tokens(system)
    messages_tokens = estimate_tokens(messages)
    tools_tokens = estimate_tokens(tools) if tools else 0

    total = system_tokens + messages_tokens + tools_tokens

    window = get_context_window(model)
    usable_input = min(window - max_output_tokens,
                      int(window * reserve_fraction))

    fits = total <= usable_input
    if fits:
        message = None
    else:
        overage = total - usable_input
        message = (
            f"Contract is too large for the selected model ({model}). "
            f"Estimated {total:,} tokens (system: {system_tokens:,}, "
            f"messages: {messages_tokens:,}, tools: {tools_tokens:,}); "
            f"the model has ~{usable_input:,} tokens available for input "
            f"(window: {window:,}, reserved {max_output_tokens:,} for output). "
            f"Overage: {overage:,} tokens. "
            f"Try a model with a larger context window, or chunk the "
            f"contract with the navigator before the review pass."
        )

    return PreflightResult(
        fits=fits,
        estimated_tokens=total,
        window=window,
        usable_input=usable_input,
        max_output_tokens=max_output_tokens,
        system_tokens=system_tokens,
        messages_tokens=messages_tokens,
        tools_tokens=tools_tokens,
        message=message,
    )


class PreflightResult:
    """Result of a preflight_check() call.

    Attributes:
        fits: True if the request fits within the model's
            input budget.
        estimated_tokens: total estimated tokens for the
            request (system + messages + tools).
        window: the model's known context window (tokens).
        usable_input: the input budget after reserving room
            for the output and applying the safety margin.
        max_output_tokens: input-side reserve for the
            model's response.
        system_tokens, messages_tokens, tools_tokens:
            per-part breakdown of the total.
        message: human-readable description of the failure
            (None when fits=True).
    """

    def __init__(
        self,
        *,
        fits: bool,
        estimated_tokens: int,
        window: int,
        usable_input: int,
        max_output_tokens: int,
        system_tokens: int,
        messages_tokens: int,
        tools_tokens: int,
        message: Optional[str],
    ):
        self.fits = fits
        self.estimated_tokens = estimated_tokens
        self.window = window
        self.usable_input = usable_input
        self.max_output_tokens = max_output_tokens
        self.system_tokens = system_tokens
        self.messages_tokens = messages_tokens
        self.tools_tokens = tools_tokens
        self.message = message

    def __repr__(self) -> str:
        if self.fits:
            return (
                f"PreflightResult(fits=True, "
                f"{self.estimated_tokens:,} / {self.usable_input:,} tokens)"
            )
        return (
            f"PreflightResult(fits=False, "
            f"{self.estimated_tokens:,} / {self.usable_input:,} tokens)"
        )


# ─── Context-window table ──────────────────────────────────
#
# Match: case-insensitive substring. First match wins, so
# order matters (longer/more specific phrases first).
#
# Numbers come from provider docs as of late 2025 / early
# 2026. Update this table when a major new model ships.

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # ── Anthropic ──
    "claude-3-7-sonnet": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude": 200_000,  # generic catch-all for "claude-*"

    # ── OpenAI ──
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o3": 200_000,
    "o4": 200_000,

    # ── Google ──
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.0-pro": 2_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini": 1_000_000,

    # ── Meta Llama ──
    "llama-3.1-405b": 128_000,
    "llama-3.1-70b": 128_000,
    "llama-3.1-8b": 128_000,
    "llama-3-70b": 8_192,
    "llama-3-8b": 8_192,
    "llama": 128_000,  # default to biggest for newer models

    # ── Mistral ──
    "mistral-large-2": 128_000,
    "mistral-large": 128_000,
    "mistral-medium": 32_000,
    "mistral-small": 32_000,
    "mistral-7b": 32_000,
    "mistral": 32_000,

    # ── Qwen ──
    "qwen-2.5-72b": 32_000,
    "qwen-2-72b": 32_000,
    "qwen-2-7b": 32_000,
    "qwen-1.5-72b": 32_000,
    "qwen-1.5-7b": 32_000,
    "qwen-long": 1_000_000,
    "qwen": 32_000,  # default to medium

    # ── DeepSeek ──
    "deepseek-v2": 32_000,
    "deepseek-v3": 64_000,
    "deepseek": 32_000,

    # ── Small / quantized / 7B-class ──
    "flash": 8_000,  # most "*-flash" named models are 7B-class
    "small": 8_000,
    "mini": 8_000,
    "nano": 8_000,
    "7b": 8_000,
    "8b": 8_000,
}

# Provide a small selection of commonly-seen OpenRouter model
# IDs as well. These all route to "the model family" by
# substring; the prefix doesn't matter:
#   "anthropic/claude-3-5-sonnet" → matches "claude-3-5-sonnet"
#   "meta-llama/llama-3.1-70b-instruct" → matches "llama-3.1-70b"
#   "qwen/qwen-2-7b-instruct" → matches "qwen-2-7b" then "qwen"
# OpenRouter also has its own slash-prefixed IDs; the substring
# matcher handles them transparently since the model name
# (after the slash) is what we look for.


# ─── Helpers ────────────────────────────────────────────────


def _estimate_string(text: str, structured: bool = False) -> int:
    """Estimate token count for a single string.

    Default `structured=False` uses a 3 chars-per-token rate
    (slightly conservative — between English at 4 and code
    at 2). Callers can pass `structured=True` for JSON / code
    content to use the 2 chars/token rate.
    """
    if not text:
        return 0
    chars_per_token = (
        STRUCTURED_CHARS_PER_TOKEN if structured else 3
    )
    return max(1, len(text) // chars_per_token)


def extract_model_from_messages(
    messages: list[dict[str, Any]],
) -> Optional[str]:
    """Best-effort: extract a model hint from messages (if
    the caller put one there). Not currently used by the
    dpo-agent, but useful for tests / debugging.
    """
    return None  # placeholder for future use
