"""Two-pass (self-refine) agent, parameterized by task.

Pass 1: full task run via tool navigation.
Pass 2: structured critique with re-read access, then refined output.

The two passes share the same DocumentTools and the same
conversation history (pass 2 sees what pass 1 read and concluded).
Pass 2 can re-read any chunk to verify findings.

The `task` parameter selects which task's prompts to load (e.g.
"dpo", "metadata"). The pass-2 critique uses the task's
`critique.md` prompt; the pass-1 reviewer uses the task's
`reviewer.md` prompt (loaded by the inner Agent).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agent import AgentConfig, _content_to_anthropic_dict
from .llm_client import LLMClient, TextBlock, ToolUseBlock, create_client

from .agent import Agent, AgentConfig, ReviewResult
from .models import resolve_model, resolve_optional_model
from .exceptions import AgentStoppedError, MaxIterationsError, ToolError
from .tasks.loader import load_prompt
from .tools import TOOLS, DocumentTools, dispatch


@dataclass
class TwoPassConfig:
    """Configuration for the two-pass agent.

    By default, both passes use the same model (medium). For high-stakes
    use, set `critique_model` to a stronger model (e.g. opus
    judging sonnet) to reduce self-evaluation bias. Models are
    resolved from env vars (DPO_AGENT_MODEL_MEDIUM/HIGH, with
    LLM_MODEL as legacy fallback).
    """
    reviewer_model: str = field(
        default_factory=lambda: resolve_model("medium", default="claude-sonnet-5")
    )
    critique_model: str | None = field(
        default_factory=lambda: resolve_optional_model("high")
    )
    max_tokens: int = 8000
    max_iterations: int = 50
    cache_ttl: str = "ephemeral"
    cache_system_prompt: bool = True


@dataclass
class TwoPassResult:
    """The result of a two-pass run."""
    pass1_review: str
    pass2_review: str
    pass1_tool_calls: int = 0
    pass2_tool_calls: int = 0
    chunks_read: list[int] = field(default_factory=list)
    elapsed_seconds: float = 0.0


class AgentTwoPass:
    """Two-pass agent parameterized by `task`.

    Internally reuses the Agent class for pass 1 (so the tool-loop
    logic is shared) and adds a pass-2 wrapper that continues the
    conversation with a critique instruction.
    """

    def __init__(
        self,
        tools: DocumentTools,
        task: str = "dpo",
        config: TwoPassConfig | None = None,
        client: LLMClient | Any | None = None,
    ):
        self.task = task
        self.tools_impl = tools
        self.config = config or TwoPassConfig()
        # Accept LLMClient OR legacy anthropic.Anthropic.
        if isinstance(client, LLMClient) or client is None:
            self.client = client or create_client()
        else:
            from .agent import _wrap_anthropic_client
            self.client = _wrap_anthropic_client(client)
        self.critique_prompt = load_prompt(task, "critique")

        # The pass-1 reviewer is a standard Agent.
        self.pass1_agent = Agent(
            tools=tools,
            task=task,
            config=AgentConfig(
                model=self.config.reviewer_model,
                max_tokens=self.config.max_tokens,
                max_iterations=self.config.max_iterations,
                cache_ttl=self.config.cache_ttl,
                cache_system_prompt=self.config.cache_system_prompt,
            ),
            client=self.client,
        )

    def run(
        self,
        document_id: str,
        defined_terms: dict[str, str] | None = None,
        parties: list[dict[str, str]] | None = None,
        governing_law_hypothesis: str | None = None,
        jurisdiction_notes: str = "",
        schema: str | None = None,
        known_metadata: dict[str, Any] | None = None,
        source_hints: str | None = None,
    ) -> TwoPassResult:
        """Run the two-pass pipeline.

        Returns:
            TwoPassResult with both outputs + observability metadata.
            The downstream consumer should use `pass2_review`.
        """
        start = time.monotonic()
        # Pass 1: full task run. Agent.run handles the tool loop.
        pass1 = self.pass1_agent.run(
            document_id=document_id,
            defined_terms=defined_terms,
            parties=parties,
            governing_law_hypothesis=governing_law_hypothesis,
            jurisdiction_notes=jurisdiction_notes,
            schema=schema,
            known_metadata=known_metadata,
            source_hints=source_hints,
        )
        # Pass 2: critique and refine. We re-run the model with the
        # critique prompt and the pass-1 conversation in history.
        pass2_text, pass2_tool_calls = self._run_critique_pass(
            document_id=document_id,
            pass1_review=pass1.review,
        )
        all_chunks = sorted(set(pass1.chunks_read))
        return TwoPassResult(
            pass1_review=pass1.review,
            pass2_review=pass2_text,
            pass1_tool_calls=pass1.tool_calls,
            pass2_tool_calls=pass2_tool_calls,
            chunks_read=all_chunks,
            elapsed_seconds=time.monotonic() - start,
        )

    def _run_critique_pass(
        self,
        document_id: str,
        pass1_review: str,
    ) -> tuple[str, int]:
        """Run pass 2 (critique and refine) starting from a
        reconstructed pass-1 conversation. Returns
        (pass2_text, pass2_tool_calls).
        """
        critique_instruction = self._build_critique_instruction(pass1_review)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self.pass1_agent._build_user_message(
                document_id=document_id,
                defined_terms=None, parties=None,
                governing_law_hypothesis=None, jurisdiction_notes="",
                schema=None, known_metadata=None, source_hints=None,
                findings_packet=None, chunks_already_read=None,
            )},
            {"role": "assistant", "content": pass1_review},
            {"role": "user", "content": critique_instruction},
        ]
        tool_calls = 0
        chunks_read: set[int] = set()

        for _ in range(self.config.max_iterations):
            response = self._call_critique(messages)
            messages.append({
                "role": "assistant",
                "content": _content_to_anthropic_dict(response.content),
            })

            if response.stop_reason == "end_turn":
                text = "".join(
                    block.text for block in response.content
                    if isinstance(block, TextBlock)
                )
                return text, tool_calls

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
                f"Critique pass stopped unexpectedly: stop_reason={response.stop_reason}"
            )

        raise MaxIterationsError(
            f"Critique pass exceeded max_iterations={self.config.max_iterations}"
        )

    def _call_critique(self, messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.critique_model or self.config.reviewer_model,
            "max_tokens": self.config.max_tokens,
            "tools": list(TOOLS),
            "messages": messages,
        }
        if self.config.cache_system_prompt:
            kwargs["system"] = [{
                "type": "text",
                "text": self.critique_prompt,
                "cache_control": {"type": self.config.cache_ttl},
            }]
        else:
            kwargs["system"] = self.critique_prompt
        return self.client.create(**kwargs)

    def _build_critique_instruction(self, prior_review: str) -> str:
        """The user-message that triggers pass 2."""
        return f"""Your prior output is below. Critique it against
the source document, then produce a refined output.

## Your prior output

```
{prior_review}
```

## What to do now

Walk through the schema (or checklist) in your system prompt and
check each item in your prior output against the source. For
anything you suspect is wrong, **use the document tools to
re-read the relevant chunks** before changing your mind. Don't
change on a hunch; re-read the source.

Output: a revised version in the same format as your prior
output. You may re-read any chunks you need. After you produce
the revised output, stop — don't include the critique itself
in the final output.
"""


# Backwards-compat alias.
DPOAgentTwoPass = AgentTwoPass
