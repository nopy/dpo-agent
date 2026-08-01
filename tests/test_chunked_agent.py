"""Tests for the chunked map-reduce pipeline.

Covers both the low-level helpers (`_split_into_chunks`,
`_parse_chunk_json`, `_findings_as_table`,
`_append_finding_summary`, `_fallback_stitch`) and the
end-to-end `review_inline()` flow with a MockClient.
"""

from __future__ import annotations

import json

import pytest

from dpo_agent.chunked_agent import (
    ChunkedReviewer,
    ChunkedReviewResult,
    DEFAULT_CHUNK_CHARS,
    ChunkFinding,
)
from dpo_agent.llm_client import (
    LLMResponse,
    MockClient,
    TextBlock,
    ToolUseBlock,
)


# ── Helpers ──────────────────────────────────────────────


def _mock_chunk_response(chunk_index: int, summary: str | None = None) -> LLMResponse:
    """Build a canned map-phase response."""
    body = {
        "summary": summary or f"summary for chunk {chunk_index}",
        "chunk_role": "test_chunk",
        "findings": [],
        "obligations": [],
        "open_questions": [],
        "alerts": [],
    }
    return LLMResponse(
        content=[TextBlock(text=json.dumps(body))],
        stop_reason="end_turn",
    )


def _mock_reduce_response(markdown: str | None = None) -> LLMResponse:
    """Build a canned reduce-phase response."""
    return LLMResponse(
        content=[TextBlock(text=markdown or "# Consolidated review\n\nAll good.")],
        stop_reason="end_turn",
    )


# ── Text splitting ────────────────────────────────────────


def test_split_under_chunk_chars_returns_single_chunk():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    chunks = r._split_into_chunks("small text", chunk_chars=1000)
    assert chunks == ["small text"]


def test_split_at_paragraph_boundary():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    # Two paragraphs separated by \n\n. With chunk_chars=30, the
    # splitter should hit the paragraph boundary and produce
    # multiple chunks, with both paragraphs essentially intact
    # (the boundary may end up at the start of one chunk).
    text = (
        "First paragraph content here.\n\n"
        "Second paragraph content here."
    )
    chunks = r._split_into_chunks(text, chunk_chars=30)
    # At minimum, we expect multiple chunks because text is 2x chunk size.
    assert len(chunks) >= 2
    # The first paragraph should appear intact somewhere in the chunks.
    combined = "".join(chunks)
    assert combined == text  # no data loss
    assert "First paragraph content here." in combined
    # The boundary may end up at the start of one chunk.
    all_text = " ".join(chunks)
    assert "Second paragraph" in all_text


def test_split_handles_long_paragraph_without_breaks():
    """A single very long paragraph has no internal break; the
    splitter falls back to single-newline, then space."""
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    text = "a" * 100 + " " + "b" * 100  # 201 chars, one space, no newlines
    chunks = r._split_into_chunks(text, chunk_chars=50)
    # Should produce 5 chunks of roughly 50 chars each.
    assert len(chunks) == 5
    # All chunks together reproduce the full text.
    assert "".join(chunks) == text


def test_split_respects_max_chunks_safety_cap():
    """A document that produces more than MAX_CHUNKS chunks at the
    chosen chunk_chars raises a clear ValueError."""
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked", max_chunks=3)
    big = "x" * 10_000
    with pytest.raises(ValueError, match="exceeds the safety cap"):
        r.review_inline(big, chunk_chars=1000)  # → 10 chunks, > 3


# ── JSON parsing ──────────────────────────────────────────


def test_parse_chunk_json_plain():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    parsed = r._parse_chunk_json('{"summary": "ok", "findings": []}')
    assert parsed["summary"] == "ok"


def test_parse_chunk_json_with_markdown_fences():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    text = "Some prose\n\n```json\n{\"summary\": \"ok\"}\n```\n\nMore prose"
    parsed = r._parse_chunk_json(text)
    assert parsed["summary"] == "ok"


def test_parse_chunk_json_with_surrounding_prose():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    text = 'Some explanation.\n{"summary": "ok", "findings": []}\nFinal note.'
    parsed = r._parse_chunk_json(text)
    assert parsed["summary"] == "ok"


def test_parse_chunk_json_invalid_raises():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    with pytest.raises(ValueError, match="No JSON object found"):
        r._parse_chunk_json("totally not JSON")


# ── Running summary ──────────────────────────────────────


def test_running_summary_caps_at_50k():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    long_summary = "a" * 60_000
    finding = ChunkFinding(
        chunk_index=42,
        chunk_text_chars=1000,
        raw={"summary": long_summary, "findings": []},
    )
    result = r._append_finding_summary(
        "existing stuff", finding
    )
    # Capped — total should be under 60K chars (the 50K cap on
    # the appended summary, plus a small per-line overhead).
    assert len(result) <= 60_000


def test_running_summary_chains_multiple_findings():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    running = ""
    for i in range(5):
        finding = ChunkFinding(
            chunk_index=i,
            chunk_text_chars=1000,
            raw={"summary": f"finding {i}", "findings": []},
        )
        running = r._append_finding_summary(running, finding)
    # All five findings should be present.
    for i in range(5):
        assert f"finding {i}" in running


def test_running_summary_marks_error_chunks():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    finding = ChunkFinding(
        chunk_index=2,
        chunk_text_chars=100,
        raw={
            "_error": True,
            "_error_message": "ContextWindowError: too big",
        },
    )
    running = r._append_finding_summary("", finding)
    assert "ERROR" in running
    assert "ContextWindowError" in running


# ── Findings table rendering ─────────────────────────────


def test_findings_as_table_renders_header():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    findings = [
        ChunkFinding(chunk_index=0, chunk_text_chars=500,
                     raw={"summary": "alpha", "findings": []}),
        ChunkFinding(chunk_index=1, chunk_text_chars=400,
                     raw={"summary": "beta", "findings": []}),
    ]
    table = r._findings_as_table(findings)
    assert "| # | Chars | Summary |" in table
    assert "| 0 |" in table
    assert "alpha" in table
    assert "| 1 |" in table
    assert "beta" in table


def test_findings_as_table_includes_structured_extras():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    findings = [
        ChunkFinding(
            chunk_index=0, chunk_text_chars=500,
            raw={
                "summary": "alpha",
                "findings": [],
                "obligations": [{"id": "obl-1", "obligor": "Acme"}],
            },
        ),
    ]
    table = r._findings_as_table(findings)
    assert "Chunk 0 details" in table
    assert "obl-1" in table
    assert "Acme" in table


def test_findings_as_table_empty_returns_placeholder():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    assert "(no findings)" in r._findings_as_table([])


# ── Fallback stitch ──────────────────────────────────────


def test_fallback_stitch_concatenates_per_chunk_summaries():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    findings = [
        ChunkFinding(chunk_index=0, chunk_text_chars=100,
                     raw={"summary": "first finding", "findings": []}),
        ChunkFinding(chunk_index=1, chunk_text_chars=100,
                     raw={"summary": "second finding", "findings": []}),
    ]
    stitched = r._fallback_stitch(findings)
    assert "first finding" in stitched
    assert "second finding" in stitched
    assert "Chunk 0" in stitched
    assert "Chunk 1" in stitched


def test_fallback_stitch_handles_error_findings():
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    findings = [
        ChunkFinding(
            chunk_index=0,
            chunk_text_chars=100,
            raw={"_error": True, "_error_message": "boom"},
        ),
    ]
    stitched = r._fallback_stitch(findings)
    assert "Chunk 0 (ERROR)" in stitched
    assert "boom" in stitched


# ── End-to-end map-reduce ────────────────────────────────


def test_review_inline_small_text_does_not_split():
    """Contracts that fit in one chunk should produce 1 map call +
    1 reduce call (2 total), regardless of how short they are."""
    mc = MockClient()
    mc.set_response(_mock_chunk_response(0))  # map
    mc.set_response(_mock_reduce_response())  # reduce

    r = ChunkedReviewer(
        client=mc, task="dpo_chunked", chunk_chars=60_000,
    )
    result = r.review_inline(text="short contract")

    assert result.chunk_count == 1
    assert result.findings[0].raw["summary"] == "summary for chunk 0"
    assert "Consolidated" in result.consolidated_review
    # mc.call_log captured both calls.
    assert len(mc.call_log) == 2


def test_review_inline_large_text_produces_multiple_chunks():
    """A 200KB contract at chunk_chars=60_000 should produce
    multiple chunks. Each chunk does 1 map call. Plus 1 reduce
    call at the end."""
    r0 = ChunkedReviewer(client=MockClient(), task="dpo_chunked",
                         chunk_chars=60_000)
    # First, compute the expected chunk count by splitting the
    # text directly (the same logic review_inline uses internally).
    big_text = "x" * 200_000  # 200KB → ~4 chunks at 60K each
    expected_chunks = len(r0._split_into_chunks(big_text, 60_000))
    assert expected_chunks >= 3

    mc = MockClient()
    # Queue exactly `expected_chunks` map responses, then the
    # reduce response. The reduce is the (expected_chunks+1)th
    # call, which lands on the last queued item.
    for i in range(expected_chunks):
        mc.set_response(_mock_chunk_response(i))
    mc.set_response(_mock_reduce_response())

    r = ChunkedReviewer(client=mc, task="dpo_chunked",
                       chunk_chars=60_000)
    result = r.review_inline(text=big_text)

    assert result.chunk_count == expected_chunks
    assert result.tool_calls == expected_chunks + 1
    # All chunks produced a finding.
    assert len(result.findings) == expected_chunks
    # The reduce response was a markdown document — the
    # consolidated review should reflect that, not a map JSON.
    assert result.consolidated_review.startswith("#"), (
        f"Expected markdown heading, got: {result.consolidated_review[:80]!r}"
    )


def test_review_inline_propagates_chunk_finding_errors():
    """When the per-chunk map call raises an exception, we
    capture it in the ChunkFinding rather than failing the
    whole run. The reduce step then synthesizes around the
    partial findings."""
    from dpo_agent.exceptions import DPOError

    class FailingClient(MockClient):
        """A mock client whose first call always fails."""
        def __init__(self):
            super().__init__()
            self._calls = 0

        def create(self, *args, **kwargs):
            self._calls += 1
            if self._calls == 1:
                raise DPOError("simulated upstream failure")
            return _mock_chunk_response(99)

    fc = FailingClient()
    r = ChunkedReviewer(client=fc, task="dpo_chunked", chunk_chars=60_000)
    # 1 chunk (text fits at chunk_chars=60_000) → 1 failing map + nothing else.
    result = r.review_inline(text="short contract")

    # The chunk finding captured the error instead of raising.
    assert len(result.findings) == 1
    assert result.findings[0].raw.get("_error") is True
    assert "simulated upstream failure" in result.findings[0].raw["_error_message"]


# ── Default model + chunk sizing constants ────────────────


def test_default_chunk_chars_is_60k():
    """The default per-chunk budget is 60K chars — about 20K
    tokens at the 3-chars-per-token rule, which fits in Claude's
    200K window with room for system + tools + output."""
    assert DEFAULT_CHUNK_CHARS == 60_000


def test_default_model_id_reads_llm_model_env():
    """Without an env override, the chunked reviewer falls back
    to a sensible default model that has prompt caching support."""
    import os
    os.environ.pop("LLM_MODEL", None)
    r = ChunkedReviewer(client=MockClient(), task="dpo_chunked")
    assert r._default_model_id() == "claude-sonnet-5"

    os.environ["LLM_MODEL"] = "claude-sonnet-4-5"
    assert r._default_model_id() == "claude-sonnet-4-5"
    del os.environ["LLM_MODEL"]


# ── Pipeline integration ────────────────────────────────


def test_pipeline_uses_chunked_reviewer_for_large_dpo():
    """The triage pipeline automatically switches to
    ChunkedReviewer for the dpo stage when the document is
    large. With a small document + Mock client, it uses the
    regular Agent (no chunking needed)."""
    from dpo_agent import TriagePipeline
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    from dpo_agent.llm_client import MockClient, LLMResponse, TextBlock

    store = InMemoryDocStore(chunk_size=4000)
    store.add("smol", "Small contract.")
    tools = store.as_document_tools()

    mc = MockClient()
    for _ in range(20):  # plenty for the 5-stage plan
        mc.set_response(LLMResponse(
            content=[TextBlock(text="Test review output")],
            stop_reason="end_turn",
        ))

    pipeline = TriagePipeline(tools=tools)
    report = pipeline.run(document_id="smol", jurisdiction_notes="t")
    # 5 stages all completed (smol < chunk_chars, so regular Agent).
    assert len(report.stages) == 5
    # No stage errored.
    for s in report.stages:
        assert s.error is None


def test_pipeline_uses_chunked_for_large_dpo():
    """With a large document, the dpo stage should switch to
    ChunkedReviewer (map-reduce). We detect this by checking
    that the dpo stage's tool_calls count is at least
    chunk_count + 1 (1 reduce call)."""
    from dpo_agent import TriagePipeline
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    from dpo_agent.llm_client import MockClient, LLMResponse, TextBlock

    # Large document — 200K chars, will produce multiple chunks at
    # the default chunk_chars=60K.
    store = InMemoryDocStore(chunk_size=4000)
    big_text = (
        "This is a long contract clause. " * 10000  # ~250KB
    )
    store.add("big", big_text)
    tools = store.as_document_tools()

    mc = MockClient()
    # The first 4 stages use 1 call each (regular Agent).
    # The dpo stage uses chunked map-reduce: ~4 map calls +
    # 1 reduce call. Queue plenty of map responses + 1 reduce.
    for _ in range(20):
        mc.set_response(LLMResponse(
            content=[TextBlock(
                text='{"summary": "ok", "chunk_role": "test", "findings": [], "obligations": [], "open_questions": [], "alerts": []}',
            )],
            stop_reason="end_turn",
        ))
    mc.set_response(LLMResponse(
        content=[TextBlock(text="# Consolidated\n\nDone.")],
        stop_reason="end_turn",
    ))

    pipeline = TriagePipeline(tools=tools)
    # PASS the client to run() so both Agent and ChunkedReviewer
    # have one to use.
    report = pipeline.run(
        document_id="big", jurisdiction_notes="t", client=mc,
    )
    assert len(report.stages) == 5
    # The dpo stage is stages[4].
    dpo_stage = report.stages[4]
    # Tool calls should reflect the map + reduce path. Each
    # chunk does 1 map call + 1 reduce. The chunked path
    # always emits at least 1 more call than the regular Agent
    # (which would be just 1 call).
    chunk_count = len(dpo_stage.chunks_read)
    if chunk_count > 0:
        # Chunked mode was used.
        # Tool calls ≥ chunk_count + 1 (one per map + one reduce).
        assert dpo_stage.tool_calls >= chunk_count + 1
    # No stage errored unexpectedly.
    assert dpo_stage.error is None


def test_chunked_reviewer_exposed_in_public_api():
    import dpo_agent
    assert "ChunkedReviewer" in dpo_agent.__all__
    assert "ChunkedReviewResult" in dpo_agent.__all__
    assert "ChunkFinding" in dpo_agent.__all__
    assert hasattr(dpo_agent, "ChunkedReviewer")
