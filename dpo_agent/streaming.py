"""DPO agent — streaming interface.

Wraps the DPO pipeline (navigator + reviewer + optional two-pass
critique) and exposes it as a stream of AgentEvent objects. The
downstream consumer can subscribe to events for progress, logging,
UI updates, etc.

Event types:
- agent_start: a stage is beginning (navigator, reviewer_pass1,
  reviewer_pass2)
- tool_call_start: the model is about to call a tool
- tool_call_complete: the tool returned (or errored)
- text_chunk: partial text from the model (optional; off by default)
- section_complete: a section of the output is finalized
- agent_complete: a stage finished successfully
- agent_error: something went wrong

Usage:
    agent = DPOStreamingAgent(tools=my_tools)
    for event in agent.review_streaming(document_id="contract-001"):
        if event.type == "tool_call_start":
            print(f"Reading {event.tool_name}...")
        elif event.type == "agent_complete":
            print(event.text)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Iterator, Literal

import anthropic

from .exceptions import AgentStoppedError, MaxIterationsError, ToolError
from .navigator import DPONavigator, NavigatorResult
from .models import resolve_model, resolve_optional_model
from .agent import Agent, AgentConfig, ReviewResult
from .tools import TOOLS, DocumentTools, dispatch
from .two_pass import DPOAgentTwoPass, TwoPassConfig, TwoPassResult


EventType = Literal[
    "agent_start",
    "tool_call_start",
    "tool_call_complete",
    "text_chunk",
    "section_complete",
    "agent_complete",
    "agent_error",
]


# Section markers the model is told to emit in the prompt. When the
# streaming text crosses one of these, we emit a section_complete
# event. This is heuristic — see module docstring.
SECTION_MARKERS = [
    "## 1. Triage",
    "## 2. Findings",
    "## 3. Obligations",
    "## 4. Open questions",
]


@dataclass
class AgentEvent:
    """A single event from the streaming agent pipeline."""
    type: EventType
    agent: str
    document_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_result: str | None = None
    section: str | None = None
    text: str | None = None
    error: str | None = None
    iteration: int = 0
    elapsed_ms: int = 0


@dataclass
class StreamingConfig:
    """Configuration for the streaming pipeline.

    Models are resolved from env vars (DPO_AGENT_MODEL_LOW/MEDIUM/HIGH,
    with LLM_MODEL as legacy fallback). The defaults:
        - navigator_model: low  (cheap, fast)
        - reviewer_model:  medium (default)
        - critique_model:  high (strong, optional)
    """
    task: str = "dpo"  # selects which task's prompts to load
    navigator_model: str = field(
        default_factory=lambda: resolve_model(
            "low", default=DPONavigator.DEFAULT_NAVIGATOR_MODEL
        )
    )
    reviewer_model: str = field(
        default_factory=lambda: resolve_model("medium", default="claude-sonnet-5")
    )
    critique_model: str | None = field(
        default_factory=lambda: resolve_optional_model("high")
    )
    max_tokens: int = 8000
    max_iterations: int = 50
    cache_ttl: str = "ephemeral"
    include_text_chunks: bool = False


class StreamingAgent:
    """Streaming wrapper around the agent pipeline, parameterized
    by `task`.

    Stages (run in order, each as a generator):
    1. Navigator — find task-relevant material (Stage 1)
    2. Reviewer — produce the final output (Stage 2)
    3. Critique — refine the output (Stage 3, optional, two-pass)

    Each stage yields AgentEvents. The consumer iterates the single
    generator and reacts to events as they arrive.
    """

    def __init__(
        self,
        tools: DocumentTools,
        task: str = "dpo",
        config: StreamingConfig | None = None,
        client: anthropic.Anthropic | None = None,
    ):
        self.tools_impl = tools
        # If the user passed a config without task set, propagate.
        if config is None:
            self.config = StreamingConfig(task=task)
        else:
            if not hasattr(config, "task") or config.task == "dpo":
                config.task = task
            self.config = config
        self.client = client or anthropic.Anthropic()

    def review_streaming(
        self,
        document_id: str,
        two_pass: bool = True,
        defined_terms: dict[str, str] | None = None,
        parties: list[dict[str, str]] | None = None,
        governing_law_hypothesis: str | None = None,
        jurisdiction_notes: str = "",
    ) -> Iterator[AgentEvent]:
        """Run the full pipeline as a generator of events.

        The final review is yielded as the last event with
        type="agent_complete" and text=... — so the consumer can
        just iterate and collect the last text.
        """
        # Stage 1: Navigator
        yield AgentEvent(
            type="agent_start", agent="navigator", document_id=document_id,
        )
        packet = None
        try:
            for event in self._navigator_streaming(
                document_id=document_id,
                defined_terms=defined_terms,
                parties=parties,
                governing_law_hypothesis=governing_law_hypothesis,
                jurisdiction_notes=jurisdiction_notes,
            ):
                yield event
                if event.type == "agent_complete" and event.section == "packet":
                    packet = event.text
        except Exception as e:
            yield AgentEvent(
                type="agent_error", agent="navigator",
                document_id=document_id, error=str(e),
            )
            return
        if not packet:
            yield AgentEvent(
                type="agent_error", agent="navigator",
                document_id=document_id, error="Navigator produced no packet",
            )
            return

        # Stage 2: Reviewer (pass 1)
        yield AgentEvent(
            type="agent_start", agent="reviewer_pass1", document_id=document_id,
        )
        pass1_text = None
        try:
            for event in self._reviewer_streaming(
                document_id=document_id, packet=packet,
            ):
                yield event
                if event.type == "agent_complete" and event.section == "pass1":
                    pass1_text = event.text
        except Exception as e:
            yield AgentEvent(
                type="agent_error", agent="reviewer_pass1",
                document_id=document_id, error=str(e),
            )
            return
        if not pass1_text:
            yield AgentEvent(
                type="agent_error", agent="reviewer_pass1",
                document_id=document_id, error="Pass 1 produced no output",
            )
            return

        # Stage 3: Critique (optional)
        if two_pass:
            yield AgentEvent(
                type="agent_start", agent="reviewer_pass2", document_id=document_id,
            )
            try:
                for event in self._critique_streaming(
                    document_id=document_id,
                    packet=packet, prior_review=pass1_text,
                ):
                    yield event
            except Exception as e:
                yield AgentEvent(
                    type="agent_error", agent="reviewer_pass2",
                    document_id=document_id, error=str(e),
                )
                return

    # --- Stage 1: Navigator streaming ---

    def _navigator_streaming(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None,
        parties: list[dict[str, str]] | None,
        governing_law_hypothesis: str | None,
        jurisdiction_notes: str,
    ) -> Iterator[AgentEvent]:
        from .tasks.loader import load_prompt
        system_prompt = load_prompt(self.config.task, "navigator")
        user_message = self._build_user_message(
            document_id=document_id, defined_terms=defined_terms,
            parties=parties, governing_law_hypothesis=governing_law_hypothesis,
            jurisdiction_notes=jurisdiction_notes,
        )
        messages = [{"role": "user", "content": user_message}]
        start = time.monotonic()

        for iteration in range(self.config.max_iterations):
            text_buf = ""
            current_tool = self._new_tool_state()
            yield from self._run_streaming_iteration(
                system_prompt=system_prompt,
                model=self.config.navigator_model,
                messages=messages,
                agent="navigator",
                document_id=document_id,
                iteration=iteration,
                start=start,
                current_tool=current_tool,
                text_buf=text_buf,
                include_text_chunks=self.config.include_text_chunks,
            )
            # After the iteration, current_tool is reset; check
            # whether the model finished or issued more tool calls.
            # The streaming loop below continues if needed.
            if current_tool["done"]:
                final_text = current_tool["text_buf"]
                yield AgentEvent(
                    type="agent_complete", agent="navigator",
                    document_id=document_id, section="packet",
                    text=final_text,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
                return
            # Otherwise, messages has been updated with the new
            # assistant + tool_result turn. Continue to the next
            # iteration.
            # (Loop condition: for-loop continues)
            # (Need a way to break out when max_iterations reached —
            # raise inside the helper.)
            _ = iteration  # silence unused
        raise MaxIterationsError(
            f"Navigator exceeded max_iterations={self.config.max_iterations}"
        )

    # --- Stage 2: Reviewer streaming ---

    def _reviewer_streaming(
        self, document_id: str, packet: str,
    ) -> Iterator[AgentEvent]:
        from .tasks.loader import load_prompt
        system_prompt = load_prompt(self.config.task, "reviewer")
        user_message = (
            f"<current_document>\ndocument_id: {document_id}\n</current_document>\n\n"
            f"<findings_packet>\n{packet}\n</findings_packet>"
        )
        messages = [{"role": "user", "content": user_message}]
        start = time.monotonic()

        for iteration in range(self.config.max_iterations):
            text_buf = ""
            current_tool = self._new_tool_state()
            sections_seen: set[str] = set()
            yield from self._run_streaming_iteration(
                system_prompt=system_prompt,
                model=self.config.reviewer_model,
                messages=messages,
                agent="reviewer_pass1",
                document_id=document_id,
                iteration=iteration,
                start=start,
                current_tool=current_tool,
                text_buf=text_buf,
                sections_seen=sections_seen,
                include_text_chunks=self.config.include_text_chunks,
            )
            if current_tool["done"]:
                final_text = current_tool["text_buf"]
                yield AgentEvent(
                    type="agent_complete", agent="reviewer_pass1",
                    document_id=document_id, section="pass1",
                    text=final_text,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
                return
        raise MaxIterationsError(
            f"Reviewer exceeded max_iterations={self.config.max_iterations}"
        )

    # --- Stage 3: Critique streaming ---

    def _critique_streaming(
        self, document_id: str, packet: str, prior_review: str,
    ) -> Iterator[AgentEvent]:
        from .tasks.loader import load_prompt
        system_prompt = load_prompt(self.config.task, "critique")
        user_message = (
            f"<current_document>\ndocument_id: {document_id}\n</current_document>\n\n"
            f"<findings_packet>\n{packet}\n</findings_packet>\n\n"
            f"<prior_review>\n{prior_review}\n</prior_review>\n\n"
            f"[Your prior review is above. Critique it against the source "
            f"document using the document tools. Output a revised review in "
            f"the same 4-section format. See your system prompt for the "
            f"detailed critique instructions.]"
        )
        messages = [{"role": "user", "content": user_message}]
        start = time.monotonic()

        for iteration in range(self.config.max_iterations):
            text_buf = ""
            current_tool = self._new_tool_state()
            yield from self._run_streaming_iteration(
                system_prompt=system_prompt,
                model=self.config.critique_model or self.config.reviewer_model,
                messages=messages,
                agent="reviewer_pass2",
                document_id=document_id,
                iteration=iteration,
                start=start,
                current_tool=current_tool,
                text_buf=text_buf,
                include_text_chunks=self.config.include_text_chunks,
            )
            if current_tool["done"]:
                final_text = current_tool["text_buf"]
                yield AgentEvent(
                    type="agent_complete", agent="reviewer_pass2",
                    document_id=document_id, section="pass2",
                    text=final_text,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
                return
        raise MaxIterationsError(
            f"Critique exceeded max_iterations={self.config.max_iterations}"
        )

    # --- Helpers ---

    def _new_tool_state(self) -> dict:
        """Per-iteration mutable state for tool-call assembly."""
        return {
            "current_tool_name": None,
            "current_tool_id": None,
            "current_tool_input_json": "",
            "tool_results": [],
            "assistant_content": [],
            "done": False,
            "text_buf": "",
            "error": None,
        }

    def _run_streaming_iteration(
        self,
        system_prompt: str,
        model: str,
        messages: list[dict],
        agent: str,
        document_id: str,
        iteration: int,
        start: float,
        current_tool: dict,
        text_buf: str,
        sections_seen: set[str] | None = None,
        include_text_chunks: bool = False,
    ) -> Iterator[AgentEvent]:
        """Run one streaming API call. Mutates `current_tool` and
        `messages` in place. Yields events as they happen.

        When the iteration completes, the caller checks
        `current_tool["done"]` to decide whether to continue
        (model issued tool calls) or finish (model produced final
        text without tool calls).
        """
        elapsed = int((time.monotonic() - start) * 1000)
        with self.client.messages.stream(
            model=model,
            max_tokens=self.config.max_tokens,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": self.config.cache_ttl},
            }],
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for ev in stream:
                if ev.type == "content_block_start":
                    block = ev.content_block
                    if block.type == "tool_use":
                        current_tool["current_tool_name"] = block.name
                        current_tool["current_tool_id"] = block.id
                        current_tool["current_tool_input_json"] = ""
                        yield AgentEvent(
                            type="tool_call_start", agent=agent,
                            document_id=document_id, tool_name=block.name,
                            tool_input={}, iteration=iteration,
                            elapsed_ms=elapsed,
                        )
                    elif block.type == "text":
                        current_tool["assistant_content"].append(block)
                elif ev.type == "content_block_delta":
                    delta = ev.delta
                    if delta.type == "text_delta":
                        current_tool["text_buf"] += delta.text
                        if include_text_chunks:
                            yield AgentEvent(
                                type="text_chunk", agent=agent,
                                document_id=document_id, text=delta.text,
                                iteration=iteration, elapsed_ms=elapsed,
                            )
                        # Section detection for the reviewer stages.
                        if sections_seen is not None:
                            for marker in SECTION_MARKERS:
                                section = marker.replace("## ", "")
                                if (marker in current_tool["text_buf"]
                                        and section not in sections_seen):
                                    sections_seen.add(section)
                                    yield AgentEvent(
                                        type="section_complete", agent=agent,
                                        document_id=document_id,
                                        section=section, iteration=iteration,
                                        elapsed_ms=elapsed,
                                    )
                    elif delta.type == "input_json_delta":
                        current_tool["current_tool_input_json"] += delta.partial_json
                elif ev.type == "content_block_stop":
                    if current_tool["current_tool_name"] is not None:
                        try:
                            tool_input = json.loads(
                                current_tool["current_tool_input_json"]
                            ) if current_tool["current_tool_input_json"] else {}
                        except json.JSONDecodeError:
                            tool_input = {}
                        try:
                            result_text = dispatch(
                                current_tool["current_tool_name"],
                                tool_input, self.tools_impl,
                            )
                            error = None
                        except ToolError as e:
                            result_text = f"Error: {e}"
                            error = str(e)
                        yield AgentEvent(
                            type="tool_call_complete", agent=agent,
                            document_id=document_id,
                            tool_name=current_tool["current_tool_name"],
                            tool_input=tool_input,
                            tool_result=result_text, error=error,
                            iteration=iteration, elapsed_ms=elapsed,
                        )
                        current_tool["tool_results"].append({
                            "type": "tool_result",
                            "tool_use_id": current_tool["current_tool_id"],
                            "content": result_text,
                            **({"is_error": True} if error else {}),
                        })
                        # Reset for next tool in same turn.
                        current_tool["current_tool_name"] = None
                        current_tool["current_tool_id"] = None
                        current_tool["current_tool_input_json"] = ""
                elif ev.type == "message_stop":
                    final_message = stream.get_final_message()
                    # The streaming SDK already appended the
                    # content blocks to current_tool["assistant_content"]
                    # for us (via content_block_start), so we
                    # reconstruct the assistant content from the
                    # final message to be safe.
                    messages.append({
                        "role": "assistant",
                        "content": final_message.content,
                    })
                    if final_message.stop_reason == "end_turn":
                        current_tool["done"] = True
                        return
                    if final_message.stop_reason == "tool_use":
                        messages.append({
                            "role": "user",
                            "content": current_tool["tool_results"],
                        })
                        # Reset for next iteration.
                        current_tool["tool_results"] = []
                        return
                    raise AgentStoppedError(
                        f"{agent} stopped unexpectedly: "
                        f"stop_reason={final_message.stop_reason}"
                    )

    def _build_user_message(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None,
        parties: list[dict[str, str]] | None,
        governing_law_hypothesis: str | None,
        jurisdiction_notes: str,
    ) -> str:
        parts = [f"<current_document>\ndocument_id: {document_id}\n</current_document>"]
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
            parts.append(f"<governing_law_hypothesis>{governing_law_hypothesis}</governing_law_hypothesis>")
        if jurisdiction_notes:
            parts.append(f"<jurisdiction_notes>{jurisdiction_notes}</jurisdiction_notes>")
        return "\n".join(parts)
