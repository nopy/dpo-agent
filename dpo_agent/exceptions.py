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


class ContextWindowError(DPOError):
    """Raised when the model's input would exceed its known
    context window.

    The dpo-agent runs a preflight token estimate before each
    API call (see `dpo_agent.token_estimation`). If the
    estimate would exceed the model's input budget, this
    exception is raised BEFORE the call goes out — saving
    the user from a 60-second wait and a confusing generic
    API error.

    The error carries enough context for the UI to suggest a
    fix (use a larger-context model, or chunk the contract
    with the navigator first):

        model: str
        estimated_tokens: int
        context_window: int
        usable_input: int
        hint: str
    """
    def __init__(
        self,
        message: str,
        *,
        model: str,
        estimated_tokens: int,
        context_window: int,
        usable_input: int,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.estimated_tokens = estimated_tokens
        self.context_window = context_window
        self.usable_input = usable_input

    @property
    def overage(self) -> int:
        return max(0, self.estimated_tokens - self.usable_input)

    def to_dict(self) -> dict:
        """Serialize for inclusion in SSE error events.

        The frontend UI uses these structured fields to render
        a more helpful error than the plain message — e.g.
        showing the overage in tokens and the model that was
        rejected.
        """
        return {
            "error": "context_window_exceeded",
            "model": self.model,
            "estimated_tokens": self.estimated_tokens,
            "context_window": self.context_window,
            "usable_input": self.usable_input,
            "overage_tokens": self.overage,
            "message": str(self),
        }  
