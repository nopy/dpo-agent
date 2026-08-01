"""LLMClient — backend-agnostic abstraction over LLM SDKs.

The dpo-agent's Agent/TwoPass/Streaming/Navigator classes
talk to an LLM through this interface, so the same code can
run against:

  - **anthropic** — the Anthropic SDK pointed at
    https://api.anthropic.com (direct)
  - **openai-compat** — the OpenAI SDK pointed at any
    OpenAI-compatible endpoint (OpenRouter, OpenAI direct,
    Together, Groq, Fireworks, local llama.cpp, etc.)
  - **mock** — a deterministic mock for tests

The abstraction is intentionally Anthropic-shaped — the dpo-agent's
existing tool-use loop, prompt caching, and streaming patterns were
designed around the Anthropic API. The OpenAI-compat backend
**translates** at the boundary so callers can keep writing
Anthropic-style code.

# Canonical request shape

```python
client.messages.create(
    model="claude-sonnet-4-5",
    system=[{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": "..."}],
    tools=[{"name": "...", "description": "...", "input_schema": {...}}],
    max_tokens=4096,
)
```

Returns an `LLMResponse` with:

```python
LLMResponse(
    content=[
        TextBlock(text="..."),
        ToolUseBlock(id="...", name="...", input={...}),
    ],
    stop_reason="end_turn" | "tool_use" | "max_tokens" | "stop_sequence",
    usage={"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 50},
    model="claude-sonnet-4-5",
)
```

# Streaming shape

```python
with client.messages.stream(model=..., messages=..., tools=..., system=...) as stream:
    for event in stream:
        # event is a StreamEvent: "message_start", "content_block_start",
        # "content_block_delta", "content_block_stop", "message_delta",
        # "message_stop", "tool_use"
        ...
    final = stream.get_final_message()
```

# Backend selection

The `create_client()` factory auto-detects from env:
  - `LLM_BACKEND` env var (explicit override: "anthropic", "openai-compat", "mock")
  - Then: if `ANTHROPIC_API_KEY` set → anthropic
  - Then: if `OPENAI_API_KEY` or `OPENROUTER_API_KEY` set → openai-compat
  - Else: mock (no API key required)
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from .exceptions import ContextWindowError, DPOError


# ─── Request / response dataclasses (the canonical shape) ─────────

# Strings that providers commonly include in their 400-error
# messages when the request exceeds the model's context window.
# Each backend parses these out and re-raises as
# ContextWindowError so the UI gets a structured error instead
# of a generic API exception.
CONTEXT_WINDOW_ERROR_PATTERNS = (
    "prompt is too long",
    "context length",
    "context_length",
    "maximum context length",
    "input is too long",
    "request too large",
    "token limit exceeded",
    "max_tokens",
    "too many tokens",
    "context window",
)


def _is_context_window_error(message: str) -> bool:
    """Detect provider-side context-window errors.

    Different providers word this differently. We match
    case-insensitive substrings against a known set rather
    than a single regex because the messages vary widely
    ("prompt is too long: 240000 tokens > 200000 maximum"
    vs "context_length_exceeded" vs "Input tokens exceed
    the maximum context length").

    Conservative: matches on the more specific phrases
    only, so we don't accidentally classify a generic 400
    as a context error.
    """
    lower = message.lower()
    return any(pat in lower for pat in CONTEXT_WINDOW_ERROR_PATTERNS)


def _resolve_window_for_error(model: str) -> int:
    """Resolve the context window for an error message.

    Some provider error bodies include the model's window
    in the message itself (e.g. "max context length is
    200000 tokens"). We don't parse the message — too
    fragile — but we do look up the model in our table for
    a sensible default. If the model isn't known, returns
    the conservative DEFAULT_MAX_TOKENS.
    """
    # Local import to avoid a circular top-level import.
    from .token_estimation import get_context_window
    return get_context_window(model)


def _extract_error_message(exc: Exception) -> str:
    """Extract a useful error message from any of the SDK's
    exception types.

    The Anthropic SDK exposes 'message' on APIStatusError;
    OpenAI's BadRequestError has 'message' directly. Some
    providers wrap errors in HTTPError; some don't. We try
    a series of accessors so the message we match against
    _is_context_window_error is the actual user-facing
    text, not the repr."""
    for attr in ("message", "body"):
        try:
            val = getattr(exc, attr)
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                # Anthropic-style: {"error": {"message": "..."}}
                err = val.get("error", {})
                if isinstance(err, dict) and "message" in err:
                    return err["message"]
                if "message" in val:
                    return val["message"]
        except AttributeError:
            continue
    return str(exc)

@dataclass
class TextBlock:
    """A text content block in the model's response."""
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    """A tool-use content block in the model's response."""
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


# Union type — internally we treat response.content as a list of these
ResponseBlock = TextBlock | ToolUseBlock


@dataclass
class Usage:
    """Token usage for a request."""
    input_tokens: int = 0
    output_tokens: int = 0
    # Anthropic's prompt caching: cache_read = tokens that hit
    # the cache (90% discount), cache_write = tokens newly cached.
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class LLMResponse:
    """A canonical LLM response. All backends return this shape.

    Fields:
        content: ordered list of TextBlock / ToolUseBlock.
        stop_reason: "end_turn" (model finished), "tool_use"
            (model wants to call a tool), "max_tokens" (truncated),
            "stop_sequence" (hit a stop string).
        usage: token usage.
        model: the model that produced the response (may differ
            from request model on OpenRouter fallbacks).
    """
    content: list[ResponseBlock]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""


# Streaming event types (a subset of Anthropic's stream events;
# OpenAI-compat translates upstream deltas to these)
STREAM_MESSAGE_START = "message_start"
STREAM_CONTENT_BLOCK_START = "content_block_start"
STREAM_CONTENT_BLOCK_DELTA = "content_block_delta"
STREAM_CONTENT_BLOCK_STOP = "content_block_stop"
STREAM_MESSAGE_DELTA = "message_delta"
STREAM_MESSAGE_STOP = "message_stop"


@dataclass
class StreamEvent:
    """A single event in a streaming response.

    The dpo-agent's streaming code only consumes a few fields:
      - event.type (above constants)
      - event.text_delta (str) — for content_block_delta of text
      - event.tool_input_json (str) — partial JSON for in-progress tool calls
      - event.usage (Usage) — for message_delta at the end
3      - event.stop_reason (str) — for message_delta at the end
    """
    type: str
    text_delta: str = ""
    tool_input_json: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    usage: Usage | None = None
    stop_reason: str = ""


# ─── Abstract base class ──────────────────────────────────────────


class LLMClient(abc.ABC):
    """Abstract base for all LLM backends.

    Subclasses implement `messages.create` and `messages.stream`
    to return LLMResponse / StreamEvent objects in the canonical
    shape. The dpo-agent's Agent classes can be written against
    this interface without caring which backend is wired in.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def create(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a messages.create request and return a canonical LLMResponse."""

    @abc.abstractmethod
    def stream(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> "LLMStreamContext":
        """Open a streaming context manager."""


class LLMStreamContext(abc.ABC):
    """A streaming response context manager.

    Usage:
        with client.messages.stream(...) as stream:
            for event in stream:
                ...
            final = stream.get_final_message()
    """

    @abc.abstractmethod
    def __iter__(self) -> Iterator[StreamEvent]: ...

    @abc.abstractmethod
    def __enter__(self) -> "LLMStreamContext":
        return self

    @abc.abstractmethod
    def __exit__(self, *exc: Any) -> None: ...

    @abc.abstractmethod
    def get_final_message(self) -> LLMResponse: ...


# ─── Anthropic backend ────────────────────────────────────────────


class AnthropicClient(LLMClient):
    """LLMClient backed by the Anthropic Python SDK.

    Translates the canonical request shape into Anthropic's
    messages.create / messages.stream call and translates
    the response back into LLMResponse / StreamEvent.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise DPOError(
                "The 'anthropic' package is required for the "
                "AnthropicClient. Install with: "
                "pip install dpo-agent[server]"
            ) from e
        self._anthropic = anthropic
        self._kwargs = kwargs
        if api_key is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise DPOError(
                "ANTHROPIC_API_KEY is not set. Set it in your "
                "environment or .env file (it's read from .env "
                "by docker-compose)."
            )
        self._client = anthropic.Anthropic(api_key=api_key, **kwargs)

    def stream(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> "_AnthropicStreamContext":
        call_kwargs: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools
        if temperature is not None:
            call_kwargs["temperature"] = temperature

        return _AnthropicStreamContext(
            self._client.messages.stream(**call_kwargs)
        )

    def create(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        call_kwargs: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools
        if temperature is not None:
            call_kwargs["temperature"] = temperature

        try:
            response = self._client.messages.create(**call_kwargs)
        except Exception as exc:
            # Providers return 400s with a generic status body
            # for context-length errors. The Anthropic SDK
            # exposes the body as .body['error']['message'] on
            # APIStatusError, and as a plain str for other
            # exception types. We extract defensively.
            err_msg = _extract_error_message(exc)
            if _is_context_window_error(err_msg):
                window = _resolve_window_for_error(model)
                raise ContextWindowError(
                    f"Model rejected the request as too long: {err_msg}",
                    model=model,
                    estimated_tokens=0,  # unknown — see preflight
                    context_window=window,
                    usable_input=window - call_kwargs.get("max_tokens", 4096),
                ) from exc
            raise
        return _anthropic_to_response(response)


def _anthropic_to_response(response: Any) -> LLMResponse:
    """Translate an Anthropic Message into LLMResponse."""
    blocks: list[ResponseBlock] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            blocks.append(TextBlock(text=block.text))
        elif getattr(block, "type", None) == "tool_use":
            blocks.append(ToolUseBlock(
                id=block.id,
                name=block.name,
                input=dict(block.input),
            ))
    usage_dict = getattr(response, "usage", None)
    usage = Usage()
    if usage_dict is not None:
        # Anthropic returns a Usage object with input_tokens, output_tokens
        usage = Usage(
            input_tokens=getattr(usage_dict, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_dict, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage_dict, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage_dict, "cache_creation_input_tokens", 0) or 0,
        )
    return LLMResponse(
        content=blocks,
        stop_reason=response.stop_reason or "end_turn",
        usage=usage,
        model=getattr(response, "model", "") or "",
    )


class _AnthropicStreamContext(LLMStreamContext):
    """Streaming context backed by anthropic.Anthropic.messages.stream."""

    def __init__(self, anthropic_stream: Any) -> None:
        self._stream_ctx = anthropic_stream
        self._stream = anthropic_stream
        self._final: LLMResponse | None = None

    def __enter__(self) -> "_AnthropicStreamContext":
        self._stream_ctx.__enter__()
        # The Anthropic SDK's stream.__enter__() returns the
        # stream; we already have it via __init__, so just return self.
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stream_ctx.__exit__(*exc)

    def __iter__(self) -> Iterator[StreamEvent]:
        # The Anthropic stream is itself iterable yielding events.
        try:
            from anthropic.types import (
                MessageStartEvent,
                ContentBlockStartEvent,
                ContentBlockDeltaEvent,
                ContentBlockStopEvent,
                MessageDeltaEvent,
                MessageStopEvent,
            )
        except ImportError:
            MessageStartEvent = ContentBlockStartEvent = ContentBlockDeltaEvent = (
                ContentBlockStopEvent
            ) = MessageDeltaEvent = MessageStopEvent = None  # type: ignore

        for event in self._stream:
            etype = getattr(event, "type", None)
            if etype == "message_start":
                yield StreamEvent(type=STREAM_MESSAGE_START)
            elif etype == "content_block_start":
                block = getattr(event, "content_block", None)
                if block and getattr(block, "type", None) == "tool_use":
                    yield StreamEvent(
                        type=STREAM_CONTENT_BLOCK_START,
                        tool_use_id=getattr(block, "id", "") or "",
                        tool_name=getattr(block, "name", "") or "",
                    )
                else:
                    yield StreamEvent(type=STREAM_CONTENT_BLOCK_START)
            elif etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is not None:
                    if getattr(delta, "type", None) == "text_delta":
                        yield StreamEvent(
                            type=STREAM_CONTENT_BLOCK_DELTA,
                            text_delta=getattr(delta, "text", "") or "",
                        )
                    elif getattr(delta, "type", None) == "input_json_delta":
                        yield StreamEvent(
                            type=STREAM_CONTENT_BLOCK_DELTA,
                            tool_input_json=getattr(delta, "partial_json", "") or "",
                        )
                    else:
                        yield StreamEvent(type=STREAM_CONTENT_BLOCK_DELTA)
            elif etype == "content_block_stop":
                yield StreamEvent(type=STREAM_CONTENT_BLOCK_STOP)
            elif etype == "message_delta":
                usage_dict = getattr(event, "usage", None)
                usage = None
                if usage_dict is not None:
                    usage = Usage(
                        output_tokens=getattr(usage_dict, "output_tokens", 0) or 0,
                    )
                yield StreamEvent(
                    type=STREAM_MESSAGE_DELTA,
                    stop_reason=getattr(event, "stop_reason", "") or "",
                    usage=usage,
                )
            elif etype == "message_stop":
                yield StreamEvent(type=STREAM_MESSAGE_STOP)
            else:
                # Unknown event type — yield a placeholder so the
                # caller can iterate without crashing.
                yield StreamEvent(type=str(etype) if etype else "unknown")

    def get_final_message(self) -> LLMResponse:
        if self._final is None:
            final = self._stream_ctx.get_final_message()
            self._final = _anthropic_to_response(final)
        return self._final


# ─── OpenAI-compat backend (OpenRouter, OpenAI direct, Together, Groq) ──


class OpenAICompatClient(LLMClient):
    """LLMClient backed by the OpenAI Python SDK.

    Works against any OpenAI-compatible endpoint:
      - OpenAI direct (https://api.openai.com/v1) when
        OPENAI_API_KEY is set and the model is an OpenAI model.
      - OpenRouter (https://openrouter.ai/api/v1) when
        OPENROUTER_API_KEY is set. OpenRouter serves OpenAI,
        Anthropic, Google, Meta models through one API.
      - Other providers via the `base_url` and `api_key` kwargs.

    The Anthropic-shaped request gets translated to OpenAI's
    chat.completions format at the boundary, and the OpenAI
    response gets translated back into LLMResponse.
    """

    name = "openai-compat"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        app_url: str | None = None,
        app_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            import openai
        except ImportError as e:
            raise DPOError(
                "The 'openai' package is required for the "
                "OpenAICompatClient. Install with: "
                "pip install dpo-agent[server]"
            ) from e
        self._openai = openai
        self._base_url = base_url
        self._app_url = app_url
        self._app_name = app_name

        if api_key is None:
            api_key = (
                os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
            )
        if not api_key:
            raise DPOError(
                "Neither OPENROUTER_API_KEY nor OPENAI_API_KEY is "
                "set. Set one in your environment or .env file."
            )
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        # OpenRouter recommends HTTP-Referer + X-Title headers
        # for app attribution. We add them only if base_url is an
        # OpenRouter endpoint.
        if base_url and "openrouter.ai" in base_url:
            default_headers = {}
            if app_url:
                default_headers["HTTP-Referer"] = app_url
            else:
                default_headers["HTTP-Referer"] = (
                    "https://github.com/nopy/dpo-agent"
                )
            if app_name:
                default_headers["X-Title"] = app_name
            else:
                default_headers["X-Title"] = "dpo-agent"
            client_kwargs["default_headers"] = default_headers
        self._client = openai.OpenAI(**client_kwargs)
        self._client_kwargs = client_kwargs

    def create(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        openai_messages, openai_tools = _anthropic_to_openai_request(
            system=system, messages=messages, tools=tools,
        )
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if openai_tools:
            call_kwargs["tools"] = openai_tools
        if temperature is not None:
            call_kwargs["temperature"] = temperature

        try:
            response = self._client.chat.completions.create(**call_kwargs)
        except Exception as exc:
            err_msg = _extract_error_message(exc)
            if _is_context_window_error(err_msg):
                window = _resolve_window_for_error(model)
                raise ContextWindowError(
                    f"Model rejected the request as too long: {err_msg}",
                    model=model,
                    estimated_tokens=0,
                    context_window=window,
                    usable_input=window - call_kwargs.get("max_tokens", 4096),
                ) from exc
            raise
        return _openai_to_response(response)

    def stream(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> "_OpenAICompatStreamContext":
        openai_messages, openai_tools = _anthropic_to_openai_request(
            system=system, messages=messages, tools=tools,
        )
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if openai_tools:
            call_kwargs["tools"] = openai_tools
        if temperature is not None:
            call_kwargs["temperature"] = temperature

        stream = self._client.chat.completions.create(**call_kwargs)
        return _OpenAICompatStreamContext(stream)


# ─── OpenAI <-> Anthropic translation helpers ──────────────


def _anthropic_to_openai_request(
    *,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Translate Anthropic-shaped request to OpenAI-shaped.

    Anthropic: messages=[{role: "user"|"assistant", content: str|list}],
               system: str|list, tools=[{name, description, input_schema}]

    OpenAI: messages=[{role: "system"|"user"|"assistant"|"tool",
                       content, tool_calls?, tool_call_id?}],
            tools=[{type: "function", function: {name, description, parameters}}]
    """
    openai_messages: list[dict[str, Any]] = []

    # System: in Anthropic this is a top-level field; in OpenAI it's
    # the first message with role="system".
    if system:
        if isinstance(system, str):
            openai_messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # Anthropic's list form with cache_control blocks —
            # concatenate the text content.
            text_parts = [
                b.get("text", "")
                for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if text_parts:
                openai_messages.append({
                    "role": "system", "content": "\n".join(text_parts)
                })

    # Messages
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, str):
                openai_messages.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Anthropic user content blocks: text + tool_result.
                text_parts: list[str] = []
                tool_results: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_result":
                        tool_call_id = block.get("tool_use_id", "")
                        tool_content = block.get("content", "")
                        if isinstance(tool_content, list):
                            tool_content = "\n".join(
                                b.get("text", "")
                                for b in tool_content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": (
                                tool_content
                                if isinstance(tool_content, str)
                                else str(tool_content)
                            ),
                        })

                if text_parts:
                    openai_messages.append({
                        "role": "user", "content": "".join(text_parts)
                    })
                openai_messages.extend(tool_results)

        elif role == "assistant":
            # Anthropic assistant content can have text + tool_use.
            if isinstance(content, str):
                openai_messages.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": _json_dumps(
                                    block.get("input", {})
                                ),
                            },
                        })

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(text_parts),
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                openai_messages.append(assistant_msg)

    # Tools: Anthropic's input_schema → OpenAI's parameters
    openai_tools: list[dict[str, Any]] | None = None
    if tools:
        openai_tools = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

    return openai_messages, openai_tools


def _json_dumps(obj: Any) -> str:
    """Serialize to JSON string."""
    import json
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return "{}"


def _openai_to_response(response: Any) -> LLMResponse:
    """Translate an OpenAI ChatCompletion into LLMResponse."""
    blocks: list[ResponseBlock] = []
    choice = response.choices[0] if response.choices else None
    stop_reason = "end_turn"
    if choice is not None:
        msg = getattr(choice, "message", None)
        if msg is not None:
            # Text content
            content_text = getattr(msg, "content", None) or ""
            if content_text:
                blocks.append(TextBlock(text=content_text))
            # Tool calls
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                func = getattr(tc, "function", None)
                if func is None:
                    continue
                # OpenAI tool_call.function.arguments is a JSON string.
                try:
                    import json
                    args = json.loads(func.arguments or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append(ToolUseBlock(
                    id=getattr(tc, "id", "") or "",
                    name=getattr(func, "name", "") or "",
                    input=args,
                ))
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "tool_calls":
                stop_reason = "tool_use"
            elif finish_reason == "length":
                stop_reason = "max_tokens"
            elif finish_reason == "stop":
                stop_reason = "end_turn"
            elif finish_reason in ("eos",):
                stop_reason = "end_turn"

    # Token usage
    usage = Usage()
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        usage = Usage(
            input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
            cache_read_input_tokens=(
                getattr(raw_usage, "cached_tokens", 0) or 0
            ),
        )

    return LLMResponse(
        content=blocks,
        stop_reason=stop_reason,
        usage=usage,
        model=getattr(response, "model", "") or "",
    )


class _OpenAICompatStreamContext(LLMStreamContext):
    """Streaming context that wraps an OpenAI stream and
    yields canonical StreamEvent objects."""

    def __init__(self, openai_stream: Any) -> None:
        self._openai_stream = openai_stream
        self._final: LLMResponse | None = None
        self._accumulated_text = ""
        self._accumulated_tool_calls: dict[str, dict[str, Any]] = {}
        self._finish_reason = "end_turn"
        self._usage = Usage()

    def __enter__(self) -> "_OpenAICompatStreamContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        # The OpenAI stream is a plain iterator; nothing to close.
        return None

    def __iter__(self) -> Iterator[StreamEvent]:
        # Emit a message_start event to match Anthropic's wire format
        yield StreamEvent(type=STREAM_MESSAGE_START)

        for chunk in self._openai_stream:
            # Final chunk (with usage only)
            if not getattr(chunk, "choices", None):
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage is not None:
                    self._usage = Usage(
                        input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
                        cache_read_input_tokens=(
                            getattr(raw_usage, "cached_tokens", 0) or 0
                        ),
                    )
                continue

            for choice in chunk.choices:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                # Text content delta
                text_delta = getattr(delta, "content", None) or ""
                if text_delta:
                    self._accumulated_text += text_delta
                    yield StreamEvent(
                        type=STREAM_CONTENT_BLOCK_DELTA,
                        text_delta=text_delta,
                    )

                # Tool-call deltas
                tool_calls = getattr(delta, "tool_calls", None) or []
                for tc in tool_calls:
                    tc_id = getattr(tc, "id", "") or ""
                    if tc_id and tc_id not in self._accumulated_tool_calls:
                        self._accumulated_tool_calls[tc_id] = {
                            "name": "",
                            "arguments": "",
                        }
                        yield StreamEvent(
                            type=STREAM_CONTENT_BLOCK_START,
                            tool_use_id=tc_id,
                            tool_name=getattr(tc.function, "name", "") or "",
                        )
                        self._accumulated_tool_calls[tc_id]["name"] = (
                            getattr(tc.function, "name", "") or ""
                        )
                    arguments_delta = (
                        getattr(tc.function, "arguments", None) or ""
                    )
                    if arguments_delta:
                        self._accumulated_tool_calls[tc_id]["arguments"] += (
                            arguments_delta
                        )
                        yield StreamEvent(
                            type=STREAM_CONTENT_BLOCK_DELTA,
                            tool_input_json=arguments_delta,
                            tool_use_id=tc_id,
                        )

                # Finish reason
                finish_reason = getattr(choice, "finish_reason", None)
                if finish_reason:
                    if finish_reason == "tool_calls":
                        self._finish_reason = "tool_use"
                    elif finish_reason == "length":
                        self._finish_reason = "max_tokens"
                    else:
                        self._finish_reason = "end_turn"

        # Emit a message_stop event to match Anthropic
        yield StreamEvent(
            type=STREAM_MESSAGE_DELTA,
            stop_reason=self._finish_reason,
            usage=self._usage,
        )
        yield StreamEvent(type=STREAM_MESSAGE_STOP)

    def get_final_message(self) -> LLMResponse:
        if self._final is None:
            blocks: list[ResponseBlock] = []
            if self._accumulated_text:
                blocks.append(TextBlock(text=self._accumulated_text))
            for tool_id, tc in self._accumulated_tool_calls.items():
                try:
                    import json
                    args = json.loads(tc["arguments"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append(ToolUseBlock(
                    id=tool_id,
                    name=tc["name"],
                    input=args,
                ))
            self._final = LLMResponse(
                content=blocks,
                stop_reason=self._finish_reason,
                usage=self._usage,
            )
        return self._final


# ─── Mock backend (no API key required) ──────────────────────


class MockClient(LLMClient):
    """A deterministic mock LLMClient. Used when no API key is
    available, and used heavily by tests.

    The mock's behavior is controlled by `set_response` /
    `set_responses` — tests set up canned responses, then make
    calls and assert on what was passed in.

    Tool-use loop: by default the mock returns a single text
    block with "Mock response." For tool-use testing, set
    `set_response(ToolUseBlock(...))`.
    """

    name = "mock"

    def __init__(self, **kwargs: Any) -> None:
        self.call_log: list[dict[str, Any]] = []
        self._responses: list[LLMResponse] = []
        self._default = LLMResponse(
            content=[TextBlock(text="Mock response.")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
            model="mock-model",
        )

    def set_response(self, *responses: LLMResponse) -> None:
        """Set canned responses. Each call appends to the queue;
        tests call this before the agent runs and pop one per
        LLM call. If you want to reset, pass .set_response() with
        no arguments (clears the queue).
        """
        self._responses.extend(responses)

    def create(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.call_log.append({
            "model": model,
            "messages": messages,
            "system": system,
            "tools": tools,
            "kwargs": kwargs,
        })
        if self._responses:
            return self._responses.pop(0)
        return self._default

    def stream(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] = "",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> "_MockStreamContext":
        # Just always end_turn with the text — streaming mock is trivial.
        return _MockStreamContext(self.create(
            model=model, system=system, messages=messages,
            tools=tools, max_tokens=max_tokens, **kwargs,
        ))


class _MockStreamContext(LLMStreamContext):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    def __enter__(self) -> "_MockStreamContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self) -> Iterator[StreamEvent]:
        yield StreamEvent(type=STREAM_MESSAGE_START)
        # Yield one text_delta per word.
        for block in self._response.content:
            if isinstance(block, TextBlock):
                words = block.text.split(" ")
                for i, w in enumerate(words):
                    delta = w if i == 0 else " " + w
                    yield StreamEvent(
                        type=STREAM_CONTENT_BLOCK_DELTA,
                        text_delta=delta,
                    )
        yield StreamEvent(
            type=STREAM_MESSAGE_DELTA,
            stop_reason=self._response.stop_reason,
            usage=self._response.usage,
        )
        yield StreamEvent(type=STREAM_MESSAGE_STOP)

    def get_final_message(self) -> LLMResponse:
        return self._response


# ─── Factory ────────────────────────────────────────────────


def create_client(
    backend: str | None = None,
    **kwargs: Any,
) -> LLMClient:
    """Create an LLMClient.

    Args:
        backend: explicit backend name: "anthropic",
            "openai-compat", "mock". If None, auto-detect from env:
                1. If `LLM_BACKEND` env var is set, use it.
                2. If ANTHROPIC_API_KEY is set: anthropic
                3. If OPENROUTER_API_KEY or OPENAI_API_KEY is set:
                   openai-compat (auto-detecting OpenRouter vs
                   OpenAI direct from the key name).
                4. Otherwise: mock (warning if neither key is set
                   and we're not in a test environment).
        **kwargs: passed to the backend's constructor (api_key,
            base_url, app_url, app_name for OpenAICompat).

    Returns:
        LLMClient instance.

    Raises:
        DPOError: if the requested backend is unknown, or if the
            selected backend is missing required credentials.
    """
    if backend is None:
        backend = os.environ.get("LLM_BACKEND")
    if backend is None:
        # Auto-detect by priority
        if os.environ.get("ANTHROPIC_API_KEY"):
            backend = "anthropic"
        elif os.environ.get("OPENROUTER_API_KEY"):
            backend = "openai-compat"
            kwargs.setdefault("base_url", "https://openrouter.ai/api/v1")
        elif os.environ.get("OPENAI_API_KEY"):
            backend = "openai-compat"
        else:
            backend = "mock"

    backend = backend.lower().strip()
    if backend in ("anthropic", "anthropic-direct"):
        return AnthropicClient(**kwargs)
    if backend in ("openai-compat", "openai", "openrouter"):
        # If base_url wasn't already set, guess from the key name.
        if backend == "openrouter" and "base_url" not in kwargs:
            kwargs["base_url"] = "https://openrouter.ai/api/v1"
        return OpenAICompatClient(**kwargs)
    if backend == "mock":
        return MockClient(**kwargs)
    raise DPOError(
        f"Unknown LLM backend: {backend!r}. "
        "Expected: 'anthropic', 'openai-compat', 'mock'."
    )


__all__ = [
    # Dataclasses
    "TextBlock",
    "ToolUseBlock",
    "Usage",
    "LLMResponse",
    "StreamEvent",
    # Stream event type constants
    "STREAM_MESSAGE_START",
    "STREAM_CONTENT_BLOCK_START",
    "STREAM_CONTENT_BLOCK_DELTA",
    "STREAM_CONTENT_BLOCK_STOP",
    "STREAM_MESSAGE_DELTA",
    "STREAM_MESSAGE_STOP",
    # Classes
    "LLMClient",
    "LLMStreamContext",
    "AnthropicClient",
    "OpenAICompatClient",
    "MockClient",
    # Factory
    "create_client",
]
