"""Exceptions for the dpo-agent package."""

from __future__ import annotations


class DPOError(Exception):
    """Base class for all dpo-agent errors."""


class ToolError(DPOError):
    """A document tool raised an error during a review.

    The agent loop catches these and feeds them back to the model
    as a `tool_result` with `is_error: True` so the model can adapt
    (e.g. re-read with a different index). Only non-tool errors
    propagate.
    """


class MaxIterationsError(DPOError):
    """The agent exceeded `max_iterations` without producing a final answer.

    Common cause: the contract is too large for the model's context,
    the navigation strategy needs tuning, or the model is in a loop.
    """


class AgentStoppedError(DPOError):
    """The model stopped with an unexpected `stop_reason` (e.g. refusal)."""


class ConfigurationError(DPOError):
    """The agent was constructed with invalid configuration."""
