"""Generic tool-using agent, parameterized by task.

A "task" is a directory under dpo_agent.tasks containing 3 system
prompts. The agent loads the right prompt based on the `task`
constructor argument. See dpo_agent.tasks.loader for the discovery
mechanism.

The agent runs a tool-use loop against an LLMClient (which can
be backed by Anthropic, OpenAI-compat / OpenRouter, or a mock).
The model emits tool calls; the dispatcher invokes the caller's
DocumentTools; the result is fed back; the model continues. The
loop terminates when the model produces a final text response
(stop_reason == "end_turn") without a tool_use block.

This is the base class. Task-specific subclasses (e.g. the
backwards-compat DPOAgent alias) configure the task name.

# Backward compat

The old code accepted `client: anthropic.Anthropic | None = None`.
For tests that pass an `anthropic.Anthropic` instance directly,
we detect this and wrap it in an `AnthropicClient`. New code
should pass an `LLMClient` (or rely on the auto-detected
factory via env vars / `create_client()`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .exceptions import (
    AgentStoppedError,
    ConfigurationError,
    MaxIterationsError,
    ToolError,
)
from .llm_client import (
    AnthropicClient,
    LLMClient,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    create_client,
)
from .tasks.loader import load_prompt
from .tools import TOOLS, DocumentTools, dispatch

# Model defaults are resolved from env vars (DPO_AGENT_MODEL_LOW /
# MEDIUM / HIGH, with LLM_MODEL as legacy fallback). See
# dpo_agent.models for the full resolution logic.
from .models import resolve_model


DEFAULT_REVIEWER_MODEL = "claude-sonnet-5"


@dataclass
class AgentConfig:
    """Configuration for an agent (any task).

    Fields:
        model: Model ID. Override per task for cost / quality
            tradeoffs (e.g. haiku for navigator, sonnet for
            reviewer, opus for critique). The backend determines
            which model IDs are valid (Anthropic vs OpenRouter,
            etc.).
        max_tokens: maximum tokens in the model's response.
        max_iterations: maximum number of tool-use iterations
            before giving up.
        cache_system_prompt: if True, the system prompt is cached
            (Anthropic prompt caching). Cuts cost ~10x for repeated
            calls with the same system prompt.
        cache_ttl: "ephemeral" (5min, default) or "1h".
        client: optional LLMClient. If None, auto-detected.
    """

    model: str = field(
        default_factory=lambda: resolve_model("medium", default=DEFAULT_REVIEWER_MODEL)
    )
    max_tokens: int = 4096
    max_iterations: int = 20
    cache_system_prompt: bool = True
    cache_ttl: str = "ephemeral"
    client: LLMClient | None = None


@dataclass
class ReviewResult:
    """The result of Agent.run()."""
    review: str
    tool_calls: int
    chunks_read: list[int]
    elapsed_seconds: float


def _build_user_message(
    document_id: str,
    *,
    defined_terms: dict[str, str] | None,
    parties: list[dict[str, str]] | None,
    governing_law_hypothesis: str | None,
    jurisdiction_notes: str,
    schema: str | None,
    known_metadata: dict[str, Any] | None,
    source_hints: list[str] | None,
    findings_packet: dict[str, Any] | None,
    chunks_already_read: list[int] | None,
) -> str:
    """Build the user message with all the context.

    The user message is just a free-form description of the
    task and the inputs (the model is going to read from the
    tools). The schema (if any) is appended as a JSON example.
    """
    parts = [f"Document ID: {document_id}"]
    if defined_terms:
        terms_str = "\n".join(
            f"  - {term}: {defn}" for term, defn in defined_terms.items()
        )
        parts.append(f"Defined terms:\n{terms_str}")
    if parties:
        parties_str = "\n".join(
            f"  - name={p.get('name')!r}, role={p.get('role')!r}"
            for p in parties
        )
        parts.append(f"Parties:\n{parties_str}")
    if governing_law_hypothesis:
        parts.append(f"Governing law hypothesis: {governing_law_hypothesis}")
    if jurisdiction_notes:
        parts.append(f"Jurisdiction notes: {jurisdiction_notes}")
    if known_metadata:
        parts.append(f"Known metadata: {known_metadata}")
    if source_hints:
        parts.append(
            "Source hints (sections to read): " + ", ".join(source_hints)
        )
    if findings_packet:
        parts.append(
            "Findings packet (from navigator): "
            + json_dumps(findings_packet)
        )
    if chunks_already_read:
        parts.append(
            f"Chunks already read: {sorted(chunks_already_read)} "
            "(skip these, you already saw them)"
        )
    if schema:
        parts.append(
            "Output schema (return JSON conforming to this):\n" + schema
        )
    parts.append("Read the document using the tools and produce your review.")
    return "\n\n".join(parts)


def json_dumps(obj: Any) -> str:
    """Serialize to JSON, used for embedding findings_packet."""
    import json
    try:
        return json.dumps(obj, indent=2)
    except (TypeError, ValueError):
        return str(obj)


def _content_to_anthropic_dict(blocks: list[Any]) -> list[dict[str, Any] | str]:
    """Convert an LLMResponse.content into the dict format that
    Anthropic-style messages.append({"role": "assistant", "content": ...})
    expects.

    Each block becomes a dict like {"type": "text", "text": "..."} or
    {"type": "tool_use", "id": "...", "name": "...", "input": {...}}.

    The LLMClient returns dataclass blocks (TextBlock, ToolUseBlock)
    on the wire — we re-serialize them into dicts so they can be
    appended to the messages list (which is in Anthropic's native
    format regardless of which backend is wired up).
    """
    out: list[dict[str, Any] | str] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            out.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": dict(block.input),
            })
        elif isinstance(block, dict):
            # Already a dict — pass through
            out.append(block)
        else:
            # Fallback — serialize as text
            out.append({"type": "text", "text": str(block)})
    return out


class Agent:
    """Generic tool-using agent.

    The agent's constructor takes a DocumentTools instance and
    a task name. It loads the appropriate system prompt from
    dpo_agent.tasks and runs the tool-use loop.

    Args:
        tools: the document tools (calls back into your chunk store).
        task: task name (e.g. "dpo", "metadata", "redline_suggest").
            See `dpo_agent.tasks.list_tasks()`.
        system_prompt: optional override for the loaded prompt.
        config: AgentConfig. If None, defaults are used (model
            resolved from DPO_AGENT_MODEL_MEDIUM env var).
        client: LLMClient. If None, auto-detected from env
            (ANTHROPIC_API_KEY → anthropic; OPENROUTER_API_KEY /
            OPENAI_API_KEY → openai-compat; otherwise mock).

    Backward compat: `client` may also be an `anthropic.Anthropic`
    instance, which gets wrapped in an AnthropicClient. This keeps
    the existing test suite working without modification.
    """

    def __init__(
        self,
        tools: DocumentTools,
        task: str = "dpo",
        system_prompt: str | None = None,
        config: AgentConfig | None = None,
        client: LLMClient | Any | None = None,
    ):
        self.task = task
        self.tools_impl = tools
        self.system_prompt = system_prompt or load_prompt(task, "reviewer")
        self.config = config or AgentConfig()
        self.client = self._normalize_client(client)
        if not self.system_prompt:
            raise ConfigurationError("system_prompt is empty")

    def _normalize_client(self, raw: Any) -> LLMClient:
        """Accept LLMClient OR a legacy anthropic.Anthropic,
        return a fresh LLMClient.
        """
        # If it's an LLMClient, use directly.
        if isinstance(raw, LLMClient):
            return raw
        # If it's an anthropic.Anthropic instance (detected
        # by class name to avoid a hard dependency on the
        # anthropic package) — wrap it in AnthropicClient.
        if raw is not None:
            cls_name = type(raw).__name__
            if cls_name == "Anthropic":
                return _wrap_anthropic_client(raw)
        # Otherwise None — auto-detect.
        if self.config.client is not None:
            return self.config.client
        return create_client()

    def run(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None = None,
        parties: list[dict[str, str]] | None = None,
        governing_law_hypothesis: str | None = None,
        jurisdiction_notes: str = "",
        schema: str | None = None,
        known_metadata: dict[str, Any] | None = None,
        source_hints: list[str] | None = None,
        findings_packet: dict[str, Any] | None = None,
        chunks_already_read: list[int] | None = None,
    ) -> ReviewResult:
        """Run the agent's tool-use loop.

        Returns:
            ReviewResult with the final review text, tool call
            count, chunks read, and elapsed time.
        """
        start = time.monotonic()
        user_message = _build_user_message(
            document_id,
            defined_terms=defined_terms,
            parties=parties,
            governing_law_hypothesis=governing_law_hypothesis,
            jurisdiction_notes=jurisdiction_notes,
            schema=schema,
            known_metadata=known_metadata,
            source_hints=source_hints,
            findings_packet=findings_packet,
            chunks_already_read=chunks_already_read,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        chunks_read: set[int] = set()
        tool_calls = 0

        for _ in range(self.config.max_iterations):
            response = self._call_model(messages)

            # Convert LLMResponse.content back into Anthropic-
            # shaped dicts so they can be appended to messages
            # regardless of which backend produced them.
            assistant_content = _content_to_anthropic_dict(response.content)
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                review_text = "".join(
                    block.text for block in response.content
                    if isinstance(block, TextBlock)
                )
                return ReviewResult(
                    review=review_text,
                    tool_calls=tool_calls,
                    chunks_read=sorted(chunks_read),
                    elapsed_seconds=time.monotonic() - start,
                )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if not isinstance(block, ToolUseBlock):
                        continue
                    tool_calls += 1
                    if block.name == "get_document_chunk_by_index":
                        idx = block.input.get("index")
                        if isinstance(idx, int):
                            chunks_read.add(idx)
                    try:
                        result_text = dispatch(
                            block.name, block.input, self.tools_impl
                        )
                        is_error = False
                    except ToolError as e:
                        result_text = f"Error: {e}"
                        is_error = True
                    except Exception as e:
                        result_text = f"Unexpected error: {e}"
                        is_error = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                        **({"is_error": True} if is_error else {}),
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            raise AgentStoppedError(
                f"Agent stopped unexpectedly: stop_reason={response.stop_reason}, "
                f"content={response.content!r}"
            )

        raise MaxIterationsError(
            f"Agent exceeded max_iterations={self.config.max_iterations} "
            "without producing a final answer."
        )

    def _call_model(self, messages: list[dict[str, Any]]) -> LLMResponse:
        """Call the LLMClient with the prepared messages."""
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "tools": list(TOOLS),
            "messages": messages,
        }
        if self.config.cache_system_prompt:
            kwargs["system"] = [{
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": self.config.cache_ttl},
            }]
        else:
            kwargs["system"] = self.system_prompt
        return self.client.create(**kwargs)


def _wrap_anthropic_client(anthropic_instance: Any) -> AnthropicClient:
    """Wrap an existing anthropic.Anthropic instance as an AnthropicClient.

    Used for backward compat — existing tests pass mock Anthropic
    instances via `client=`. We use `__new__` + manual attribute
    set to avoid having to instantiate a fresh AnthropicClient
    (which would try to read ANTHROPIC_API_KEY from the env, which
    the test might not have set).
    """
    wrapper = AnthropicClient.__new__(AnthropicClient)
    wrapper._anthropic = type(anthropic_instance).__module__ and (
        __import__("anthropic")
    )
    wrapper._client = anthropic_instance
    wrapper._kwargs = {}
    return wrapper


# ─────────────────────────────────────────────────────────────────────


# Backward-compat aliases (preserved from before the refactor)
def DPOAgent(
    tools: DocumentTools,
    system_prompt: str | None = None,
    config: AgentConfig | None = None,
    client: Any | None = None,
) -> Agent:
    """Legacy DPOAgent factory — returns an Agent with task="dpo"."""
    return Agent(tools=tools, task="dpo", system_prompt=system_prompt,
                 config=config, client=client)


__all__ = [
    "Agent",
    "AgentConfig",
    "ReviewResult",
    "DPOAgent",
    "DEFAULT_REVIEWER_MODEL",
]
