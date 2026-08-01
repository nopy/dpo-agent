"""Generic document navigator, parameterized by task.

The navigator produces a structured findings packet from a large
document. The packet is the input to the next stage (Agent.run
with a `findings_packet`), which never sees the document directly.

The navigator uses the same 4 document tools as the reviewer but a
different system prompt. It classifies chunks by relevance to the
task's schema/checklist and extracts verbatim excerpts — it does
NOT do the final task (no JSON for metadata, no DPO review).

For very large contracts, using the navigator + Agent pipeline is
significantly cheaper than letting the agent navigate the
document itself. The navigator uses a cheap model; the agent
uses the strong model but only reads a curated packet, not the
full contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agent import AgentConfig, _content_to_anthropic_dict, _run_preflight_or_raise
from .exceptions import AgentStoppedError, MaxIterationsError, ToolError
from .llm_client import LLMClient, create_client
from .tasks.loader import load_prompt
from .tools import TOOLS, DocumentTools, dispatch


@dataclass
class NavigatorResult:
    """The result of a navigator run."""
    packet: str
    tool_calls: int = 0
    chunks_read: list[int] = field(default_factory=list)
    elapsed_seconds: float = 0.0


class Navigator:
    """Document navigator parameterized by `task`.

    The navigator is the same tool-using agent pattern as the
    main Agent, but with a different system prompt
    (classification, not final task) and a different output
    format (packet, not review).
    """

    DEFAULT_NAVIGATOR_MODEL = "claude-haiku-4-5"

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
        self.system_prompt = system_prompt or load_prompt(task, "navigator")
        self.config = config or AgentConfig(
            model=self.DEFAULT_NAVIGATOR_MODEL,
        )
        # Accept LLMClient OR legacy anthropic.Anthropic.
        if isinstance(client, LLMClient) or client is None:
            self.client = client or create_client()
        else:
            # Wrap a legacy anthropic.Anthropic instance.
            from .agent import _wrap_anthropic_client
            self.client = _wrap_anthropic_client(client)

    def navigate(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None = None,
        parties: list[dict[str, str]] | None = None,
        governing_law_hypothesis: str | None = None,
        jurisdiction_notes: str = "",
        schema: str | None = None,
        known_metadata: dict[str, Any] | None = None,
        source_hints: str | None = None,
    ) -> NavigatorResult:
        """Run the navigator and return the packet."""
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
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        chunks_read: set[int] = set()
        tool_calls = 0

        for _ in range(self.config.max_iterations):
            response = self._call_model(messages)
            messages.append({
                "role": "assistant",
                "content": _content_to_anthropic_dict(response.content),
            })

            if response.stop_reason == "end_turn":
                from .llm_client import TextBlock
                packet_text = "".join(
                    block.text for block in response.content
                    if isinstance(block, TextBlock)
                )
                return NavigatorResult(
                    packet=packet_text,
                    tool_calls=tool_calls,
                    chunks_read=sorted(chunks_read),
                    elapsed_seconds=time.monotonic() - start,
                )

            if response.stop_reason == "tool_use":
                from .llm_client import ToolUseBlock
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
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                        **({"is_error": True} if is_error else {}),
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            raise AgentStoppedError(
                f"Navigator stopped unexpectedly: stop_reason={response.stop_reason}"
            )

        raise MaxIterationsError(
            f"Navigator exceeded max_iterations={self.config.max_iterations}"
        )

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
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

        # Same context-window preflight as Agent._call_model().
        # Raises ContextWindowError immediately if the navigator's
        # prompt + accumulated chunks would exceed the model's
        # input budget. (The navigator reads chunks — its context
        # grows monotonically across the loop iterations.)
        _run_preflight_or_raise(
            model=self.config.model,
            system=kwargs["system"],
            messages=messages,
            tools=kwargs["tools"],
            max_output_tokens=self.config.max_tokens,
        )
        return self.client.create(**kwargs)

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
    ) -> str:
        parts: list[str] = [
            f"<current_document>\ndocument_id: {document_id}\n</current_document>"
        ]
        if defined_terms:
            parts.append("<defined_terms>")
            for t, d in defined_terms.items():
                parts.append(f'<term name="{t}">{d}</term>')
            parts.append("</defined_terms>")
        if parties:
            parts.append("<parties>")
            for p in parties:
                parts.append(f'<party name="{p["name"]}" role="{p.get("role", "")}"/>')
            parts.append("</parties>")
        if governing_law_hypothesis:
            parts.append(
                f"<governing_law_hypothesis>{governing_law_hypothesis}"
                f"</governing_law_hypothesis>"
            )
        if jurisdiction_notes:
            parts.append(f"<jurisdiction_notes>{jurisdiction_notes}</jurisdiction_notes>")
        if schema:
            parts.append(f"<schema>\n{schema}\n</schema>")
        if known_metadata:
            parts.append("<known_metadata>")
            for key, value in known_metadata.items():
                parts.append(f'<field name="{key}">{value}</field>')
            parts.append("</known_metadata>")
        if source_hints:
            parts.append(f"<source_hints>\n{source_hints}\n</source_hints>")
        return "\n".join(parts)


# Backwards-compat alias.
DPONavigator = Navigator
