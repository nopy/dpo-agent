"""Generic tool-using agent, parameterized by task.

A "task" is a directory under dpo_agent.tasks containing 3 system
prompts. The agent loads the right prompt based on the `task`
constructor argument. See dpo_agent.tasks.loader for the discovery
mechanism.

The agent runs an Anthropic tool-use loop. The model emits tool
calls; the dispatcher invokes the caller's DocumentTools; the
result is fed back; the model continues. The loop terminates
when the model produces a final text response (stop_reason ==
"end_turn") without a tool_use block.

This is the base class. Task-specific subclasses (e.g. the
backwards-compat DPOAgent alias) configure the task name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .exceptions import (
    AgentStoppedError,
    ConfigurationError,
    MaxIterationsError,
    ToolError,
)
from .tasks.loader import load_prompt
from .tools import TOOLS, DocumentTools, dispatch


DEFAULT_REVIEWER_MODEL = "claude-sonnet-5"


@dataclass
class AgentConfig:
    """Configuration for an agent (any task).

    Fields:
        model: Anthropic model ID. Override per task for cost /
            quality tradeoffs (e.g. haiku for navigator, sonnet for
            reviewer, opus for high-stakes critique).
        max_tokens: max output tokens per model call.
        max_iterations: safety bound on the agent loop. The agent
            stops with MaxIterationsError if it doesn't reach
            `end_turn` after this many iterations.
        cache_ttl: prompt-cache TTL for the system prompt.
            "ephemeral" (5 min) or "1h" for batch workloads.
        cache_system_prompt: whether to set `cache_control` on
            the system prompt.
    """
    model: str = DEFAULT_REVIEWER_MODEL
    max_tokens: int = 8000
    max_iterations: int = 50
    cache_ttl: str = "ephemeral"
    cache_system_prompt: bool = True


@dataclass
class ReviewResult:
    """The result of a single agent run.

    Fields:
        review: the agent's output text. For the DPO task this
            is the review (Triage / Findings / etc.); for the
            metadata task this is the JSON object.
        tool_calls: number of tool calls the agent made.
        chunks_read: sorted list of chunk indexes the agent
            actually read. Useful for observability and for
            downstream agents to know which chunks were
            inspected.
        elapsed_seconds: total time for the run.
    """
    review: str
    tool_calls: int = 0
    chunks_read: list[int] = field(default_factory=list)
    elapsed_seconds: float = 0.0


class Agent:
    """Tool-using agent parameterized by `task`.

    Example:
        from dpo_agent import Agent, DocumentTools

        tools = DocumentTools(
            get_document_size=lambda d: len(my_store.get(d)),
            retrieve_whole_document_content=lambda d: my_store.get(d),
            get_number_of_chunks=lambda d: my_store.chunk_count(d),
            get_document_chunk_by_index=lambda d, i: my_store.get_chunk(d, i),
        )
        agent = Agent(tools=tools, task="dpo")
        result = agent.run(document_id="contract-001")
        print(result.review)
    """

    def __init__(
        self,
        tools: DocumentTools,
        task: str = "dpo",
        system_prompt: str | None = None,
        config: AgentConfig | None = None,
        client: anthropic.Anthropic | None = None,
    ):
        self.task = task
        self.tools_impl = tools
        # If the caller passes a system_prompt, use it. Otherwise
        # load the task's reviewer prompt.
        self.system_prompt = system_prompt or load_prompt(task, "reviewer")
        self.config = config or AgentConfig()
        self.client = client or anthropic.Anthropic()
        if not self.system_prompt:
            raise ConfigurationError("system_prompt is empty")

    def run(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None = None,
        parties: list[dict[str, str]] | None = None,
        governing_law_hypothesis: str | None = None,
        jurisdiction_notes: str = "",
        # The following are task-specific; pass-through to the
        # user message.
        # For the metadata task: the schema as a string
        # (JSON or schema description).
        schema: str | None = None,
        # For the metadata task: pre-known metadata values
        # from a CLM or prior extraction.
        known_metadata: dict[str, Any] | None = None,
        # For the metadata task: source hints (governing law,
        # document type, etc.).
        source_hints: str | None = None,
        # Two-stage pipeline: the navigator's packet becomes
        # the agent's primary input.
        findings_packet: str | None = None,
        chunks_already_read: list[int] | None = None,
    ) -> ReviewResult:
        """Run the agent on `document_id`.

        Returns:
            ReviewResult with the agent's output, tool-call count,
            and observability metadata.
        """
        start = time.monotonic()
        user_message = self._build_user_message(
            document_id=document_id,
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
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                review_text = "".join(
                    block.text for block in response.content
                    if block.type == "text"
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
                    if block.type == "tool_use":
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
            f"without producing a final answer."
        )

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "tools": TOOLS,
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
        return self.client.messages.create(**kwargs)

    def _build_user_message(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None,
        parties: list[dict[str, str]] | None,
        governing_law_hypothesis: str | None,
        jurisdiction_notes: str,
        schema: str | None,
        known_metadata: dict[str, Any] | None,
        source_hints: str | None,
        findings_packet: str | None,
        chunks_already_read: list[int] | None,
    ) -> str:
        """Build the user message for the first (and only) turn.

        Includes the standard context (document_id, defined
        terms, parties, governing-law hypothesis) and any
        task-specific context (schema, known_metadata,
        source_hints) and the two-stage-pipeline fields
        (findings_packet, chunks_already_read).
        """
        parts: list[str] = [
            f"<current_document>\ndocument_id: {document_id}\n</current_document>"
        ]

        if defined_terms:
            parts.append("<defined_terms>")
            for term, defn in defined_terms.items():
                parts.append(f'<term name="{term}">{defn}</term>')
            parts.append("</defined_terms>")

        if parties:
            parts.append("<parties>")
            for p in parties:
                name = p.get("name", "")
                role = p.get("role", "")
                parts.append(f'<party name="{name}" role="{role}"/>')
            parts.append("</parties>")

        if governing_law_hypothesis:
            parts.append(
                f"<governing_law_hypothesis>{governing_law_hypothesis}"
                f"</governing_law_hypothesis>"
            )

        if jurisdiction_notes:
            parts.append(f"<jurisdiction_notes>{jurisdiction_notes}</jurisdiction_notes>")

        # Task-specific context (schema, known_metadata,
        # source_hints) is included verbatim if provided. The
        # task's reviewer prompt instructs the model where these
        # appear in the user message.
        if schema:
            parts.append(f"<schema>\n{schema}\n</schema>")

        if known_metadata:
            parts.append("<known_metadata>")
            for key, value in known_metadata.items():
                parts.append(f'<field name="{key}">{value}</field>')
            parts.append("</known_metadata>")

        if source_hints:
            parts.append(f"<source_hints>\n{source_hints}\n</source_hints>")

        # Two-stage pipeline: navigator's packet is the primary input.
        if findings_packet:
            parts.append("<findings_packet>")
            parts.append(
                "[This is the navigator's packet — your primary view of the "
                "contract. You do NOT need to read the source contract; the "
                "packet contains every relevant excerpt extracted verbatim by "
                "the navigator, indexed by the schema/checklist. If you "
                "need to verify a specific quote or cross-reference, you may "
                "use the document tools to re-read chunks, but the packet "
                "should be sufficient for 90%+ of the review.]\n"
            )
            parts.append(findings_packet)
            parts.append("</findings_packet>")

        if chunks_already_read:
            parts.append(
                f"<chunks_already_inspected>{chunks_already_read}"
                f"</chunks_already_inspected>"
            )

        return "\n".join(parts)


# Backwards-compat alias. Pre-refactor code that used
# DPOAgent(tools=...) still works.
DPOAgent = Agent
