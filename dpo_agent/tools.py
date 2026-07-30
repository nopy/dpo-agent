"""Document tools — the 4 tool definitions and their dispatcher.

The agents (reviewer, navigator, two-pass) all use the same 4 tools.
The tool schema is a single module-level constant so the model sees
consistent descriptions; the dispatcher is a single function so the
error-handling logic is in one place.

Caller wires the actual implementations via the DocumentTools
dataclass. The implementations can be anything — in-memory maps for
tests, a database, an S3 bucket, a CLM API, a vector store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .exceptions import ToolError


# Size threshold for "small enough to read whole". Below this,
# the model is told to use retrieve_whole_document_content; above,
# it must use chunk-based reading.
WHOLE_DOC_MAX_CHARS = 80_000


# The tool schema — passed to every Anthropic API call. The
# descriptions are concise because they're injected on every turn.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_document_size",
        "description": (
            "Return the total character count of a document. "
            "ALWAYS call this before any read tool to determine "
            "whether to use whole-document or chunk-based reading. "
            "Threshold: < 80K chars = whole doc; > 80K = chunks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The document to size.",
                }
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "retrieve_whole_document_content",
        "description": (
            "Return the full text of a small document. ONLY call "
            "after get_document_size confirms < 80K chars. For "
            "larger documents, use get_document_chunk_by_index "
            "instead — calling this on a large document will "
            "exceed the context limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"}
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "get_number_of_chunks",
        "description": (
            "Return the number of chunks a document has been split "
            "into. Chunks are roughly section-aware but boundaries "
            "are imperfect — a section may span multiple chunks. "
            "Call this to plan your reading budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"}
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "get_document_chunk_by_index",
        "description": (
            "Return the text of a specific chunk (0-indexed). "
            "Read in any order; you may revisit chunks. For "
            "sections that span multiple chunks, read consecutive "
            "indexes together."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0-based chunk index.",
                },
            },
            "required": ["document_id", "index"],
        },
    },
]


@dataclass
class DocumentTools:
    """Caller-provided implementations of the four document tools.

    The agent calls these by name. The agent never sees the
    implementation — it just gets the return value back as a
    `tool_result`. Implementations can be:

    - In-memory dicts (for tests)
    - A database (Postgres, SQLite with FTS5)
    - An object store (S3, GCS)
    - A document management API (CLM, contract repository)
    - A vector store (Pinecone, Weaviate) with chunked retrieval

    All four callables must be synchronous and return strings (or
    ints for get_document_size / get_number_of_chunks). They may
    raise any exception; the dispatcher converts it to a tool
    result with `is_error: True` so the model can recover.
    """

    get_document_size: Callable[[str], int]
    retrieve_whole_document_content: Callable[[str], str]
    get_number_of_chunks: Callable[[str], int]
    get_document_chunk_by_index: Callable[[str, int], str]


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    tools: DocumentTools,
) -> str:
    """Run a tool call and return the result as a string for the model.

    Validates inputs, enforces the size threshold on
    `retrieve_whole_document_content`, and returns a structured
    error string if the call fails. The agent loop then feeds the
    result (or error) back to the model as a `tool_result` block.
    """
    if tool_name == "get_document_size":
        doc_id = _required(tool_input, "document_id")
        try:
            size = tools.get_document_size(doc_id)
        except Exception as e:
            raise ToolError(f"get_document_size failed: {e}") from e
        return f"Document size: {size} characters ({size // 4} tokens approx.)"

    if tool_name == "retrieve_whole_document_content":
        doc_id = _required(tool_input, "document_id")
        # Enforce the size threshold — calling this on a large doc
        # would blow the model's context. The threshold is also
        # described in the tool schema, but we double-check here
        # because the model can be wrong.
        size = tools.get_document_size(doc_id)
        if size > WHOLE_DOC_MAX_CHARS:
            raise ToolError(
                f"Document is {size} characters; above the {WHOLE_DOC_MAX_CHARS} "
                f"threshold for whole-document reading. Use "
                f"get_document_chunk_by_index instead."
            )
        try:
            content = tools.retrieve_whole_document_content(doc_id)
        except Exception as e:
            raise ToolError(f"retrieve_whole_document_content failed: {e}") from e
        return content

    if tool_name == "get_number_of_chunks":
        doc_id = _required(tool_input, "document_id")
        try:
            n = tools.get_number_of_chunks(doc_id)
        except Exception as e:
            raise ToolError(f"get_number_of_chunks failed: {e}") from e
        return f"Number of chunks: {n}"

    if tool_name == "get_document_chunk_by_index":
        doc_id = _required(tool_input, "document_id")
        idx = tool_input.get("index")
        if not isinstance(idx, int) or idx < 0:
            raise ToolError(
                f"index must be a non-negative integer, got {idx!r}"
            )
        try:
            total = tools.get_number_of_chunks(doc_id)
        except Exception as e:
            raise ToolError(f"get_number_of_chunks failed: {e}") from e
        if idx >= total:
            raise ToolError(
                f"Chunk index {idx} out of range (document has "
                f"{total} chunks, valid indexes are 0 to {total - 1})"
            )
        try:
            chunk = tools.get_document_chunk_by_index(doc_id, idx)
        except Exception as e:
            raise ToolError(f"get_document_chunk_by_index failed: {e}") from e
        # Annotate so the model knows where it is in the document.
        return f"[Chunk {idx} of {total}]\n{chunk}"

    raise ToolError(f"Unknown tool: {tool_name!r}")


def _required(d: dict[str, Any], key: str) -> Any:
    """Get a required key from a dict, or raise ToolError."""
    if key not in d:
        raise ToolError(f"Missing required parameter: {key!r}")
    return d[key]
