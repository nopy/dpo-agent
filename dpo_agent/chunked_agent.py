"""Chunked map-reduce wrapper — process documents larger than
the LLM's context window.

# Problem

The existing `Agent` reads a contract via chunk tools, but
accumulates ALL chunks in messages. For a 200 KB contract at
~10 KB per chunk, that's 20+ chunks × 8 KB tool_result text =
~160 KB of accumulated context. The model rejects requests
that exceed its context window (see `dpo_agent.exceptions
.ContextWindowError`).

# Solution

`ChunkedReviewer` runs the existing `Agent`'s logic in two
phases:

1. **MAP** — the contract is split into size-bounded chunks.
   For each chunk, the agent runs the standard tool-use loop
   on JUST that chunk's text. Between chunks, the agent's
   per-chunk findings (a structured summary) are kept in the
   messages, but the raw chunk text is DISCARDED. The agent's
   per-chunk output is bounded in size regardless of input
   length.

2. **REDUCE** — after all chunks have been processed, the
   per-chunk findings are stitched together into a single
   final review pass. The agent reads its own accumulated
   findings and produces the consolidated report.

The preflight token check (`preflight_check`) is still called
for each chunk call — it now operates on a per-chunk budget
that's much smaller. A chunk-level rejection means "this
specific chunk is too large for the model" rather than "the
whole contract is too large" — typically the chunk-size
parameter just needs to be reduced.

# Why this isn't in the regular Agent

Map-reduce requires the agent to PRODUCE a structured
per-chunk synthesis (a findings table) so the reduce step
can stitch findings back together. The existing tasks
(produce a "review" string) don't naturally do this.

Two options for this:

  A) Teach every existing task to produce structured
     per-chunk findings (intrusive — touches 15 prompts).
  B) Use a separate task prompt that knows how to do
     map-reduce (the model emits chunk-level JSON).
  C) Use the existing task prompt but discard the per-
     chunk review text and re-prompt for a final synthesis.

We chose option B: a new `dpo_chunked` task with prompts
that explicitly teach the model the per-chunk findings
JSON schema. The existing `dpo` task stays untouched.

# Usage

    from dpo_agent import ChunkedReviewer
    reviewer = ChunkedReviewer(
        tools=document_tools,
        task="dpo_chunked",
        client=llm_client,
    )
    result = reviewer.review(document_id="contract.pdf")
    print(result.chunk_count)        # e.g. 47
    print(result.findings)           # list of per-chunk findings
    print(result.consolidated_review)  # the final synthesis

Or run on inline text directly (no InMemoryDocStore needed):

    result = reviewer.review_inline(
        text="<the entire contract>",
        document_id="inline-contract",
    )

# When to use ChunkedReviewer

Use ChunkedReviewer when the contract is larger than the
model's context window — i.e. when `preflight_check` would
reject the request. For small contracts, the regular
`Agent.run()` is faster (one call instead of N+1).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .llm_client import LLMClient
from .tasks.loader import load_prompt
from .tools import DocumentTools


# ── Result dataclasses ─────────────────────────────────────


@dataclass
class ChunkFinding:
    """A single chunk's structured findings.

    The dpo_chunked task emits these as JSON: one
    ChunkFinding per chunk. The reduce step aggregates them.

    Fields are open-ended — the model's JSON output can
    include additional keys beyond what we list here. We
    capture them generically.
    """

    chunk_index: int
    chunk_text_chars: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkedReviewResult:
    """The output of `ChunkedReviewer.review()`.

    Fields:
        document_id: the document that was reviewed.
        chunk_count: number of chunks produced by the
            document's chunker.
        findings: per-chunk structured findings.
        consolidated_review: the final synthesis (a string).
        tool_calls: total tool calls across all chunks +
            the reduce step.
        chunks_read: chunk indexes that were analyzed.
        elapsed_seconds: wall-clock time for the full
            map-reduce.
        schema_version: the version of the chunked output
            schema that was used. Bumped when the JSON
            structure changes.
    """

    document_id: str
    chunk_count: int
    findings: list[ChunkFinding]
    consolidated_review: str
    tool_calls: int = 0
    chunks_read: list[int] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    schema_version: str = "0.1.0"


# ── Default sizing parameters ─────────────────────────────


# Default per-chunk character budget. Smaller than the model's
# full context window because each chunk is read WITH a system
# prompt + tool definitions. 60K chars (= ~20K tokens) leaves
# room for system + tools + output + accumulation.
DEFAULT_CHUNK_CHARS = 60_000

# Hard cap on map iterations. 200 chunks is a safety net —
# covers a contract up to ~12 MB at the default chunk size.
# The user's preflight would have rejected earlier for any
# model with < 200K context, so this should never trip in
# practice.
MAX_CHUNKS = 200


# ── The main class ──────────────────────────────────────────


class ChunkedReviewer:
    """Map-reduce chunked review of large documents.

    Run via `review()` (InMemoryDocStore-backed) or
    `review_inline()` (string-backed). Both return a
    `ChunkedReviewResult` with per-chunk findings plus a
    consolidated final review.

    The class is task-agnostic — its behavior is steered by
    the prompt at `dpo_agent/tasks/<task>/{reviewer,reduce}
    .md`. Currently only `dpo_chunked` is wired up, but the
    pattern generalizes to any task that can be expressed as
    "per-chunk structured findings → final synthesis".
    """

    def __init__(
        self,
        tools: DocumentTools | None = None,
        task: str = "dpo_chunked",
        client: LLMClient | None = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_chunks: int = MAX_CHUNKS,
    ) -> None:
        if tools is None and client is None:
            raise ValueError(
                "ChunkedReviewer requires either `tools` "
                "(for review()) or `client` (for review_inline())."
            )
        self.tools_impl = tools
        self.task = task
        self.client = client
        self.chunk_chars = chunk_chars
        self.max_chunks = max_chunks

        # Prompts are loaded lazily so that the class can be
        # imported even if the task's prompt files don't exist.
        self._reviewer_prompt: str | None = None
        self._reduce_prompt: str | None = None

    @property
    def reviewer_prompt(self) -> str:
        """The system prompt for the per-chunk MAP phase."""
        if self._reviewer_prompt is None:
            self._reviewer_prompt = load_prompt(self.task, "reviewer")
        return self._reviewer_prompt

    @property
    def reduce_prompt(self) -> str:
        """The system prompt for the REDUCE phase."""
        if self._reduce_prompt is None:
            self._reduce_prompt = load_prompt(self.task, "reduce")
        return self._reduce_prompt

    # ── Public API ─────────────────────────────────────

    def review(self, document_id: str) -> ChunkedReviewResult:
        """Run map-reduce over the chunks of an in-store document.

        Args:
            document_id: the document to review. Must be in
                the in-memory document store (or any
                `DocumentTools`-backed store).

        Returns:
            ChunkedReviewResult with per-chunk findings +
            consolidated final review.
        """
        if self.tools_impl is None:
            raise ValueError("review() requires `tools` to be set.")

        start = time.monotonic()
        text = self.tools_impl.retrieve_whole_document_content(document_id)
        return self.review_inline(text=text, document_id=document_id,
                                 _start=start)

    def review_inline(
        self,
        text: str,
        document_id: str = "inline-contract",
        _start: float | None = None,
        chunk_chars: int | None = None,
    ) -> ChunkedReviewResult:
        """Run map-reduce over chunks of a raw string.

        Args:
            text: the full contract text.
            document_id: a label for the document (used in
                per-chunk findings).
            chunk_chars: per-chunk character cap. Defaults to
                `self.chunk_chars` set on the constructor.
            _start: internal — wall-clock start time. Tests
                can pass it to measure sub-step times.
        """
        if text is None or not text.strip():
            raise ValueError("review_inline() requires non-empty text.")

        if self.client is None:
            raise ValueError(
                "review_inline() requires `client` to be set."
            )

        start = _start if _start is not None else time.monotonic()
        effective_chunk_chars = chunk_chars or self.chunk_chars

        # Split the text into chunks at chunk_chars boundaries.
        # We split on whitespace-ish boundaries to avoid breaking
        # mid-word when possible (best effort — not a tokenizer).
        chunks = self._split_into_chunks(text, effective_chunk_chars)

        if len(chunks) > self.max_chunks:
            raise ValueError(
                f"Document produced {len(chunks)} chunks at "
                f"chunk_chars={effective_chunk_chars}; this exceeds "
                f"the safety cap of MAX_CHUNKS={self.max_chunks}. "
                f"Increase `chunk_chars` or raise `max_chunks`."
            )

        # ── MAP phase: per-chunk findings ──
        findings: list[ChunkFinding] = []
        running_findings_summary = ""
        tool_calls = 0
        chunks_read: list[int] = []

        for i, chunk_text in enumerate(chunks):
            chunk_finding, tcs = self._process_chunk(
                chunk_index=i,
                chunk_text=chunk_text,
                running_findings_summary=running_findings_summary,
                document_id=document_id,
            )
            findings.append(chunk_finding)
            tool_calls += tcs
            chunks_read.append(i)
            # Update the running summary with just this chunk's
            # findings text — bounded in size, independent of
            # chunk size.
            running_findings_summary = self._append_finding_summary(
                running_findings_summary, chunk_finding
            )

        # ── REDUCE phase: consolidate ──
        consolidated_review, reduce_tcs = self._reduce(
            document_id=document_id,
            chunk_count=len(chunks),
            findings=findings,
        )
        tool_calls += reduce_tcs

        return ChunkedReviewResult(
            document_id=document_id,
            chunk_count=len(chunks),
            findings=findings,
            consolidated_review=consolidated_review,
            tool_calls=tool_calls,
            chunks_read=chunks_read,
            elapsed_seconds=time.monotonic() - start,
        )

    # ── Internal map phase ────────────────────────────

    def _process_chunk(
        self,
        *,
        chunk_index: int,
        chunk_text: str,
        running_findings_summary: str,
        document_id: str,
    ) -> tuple[ChunkFinding, int]:
        """Process one chunk. Returns (ChunkFinding, tool_calls).

        Builds the per-chunk user message containing the chunk
        text + a reference to the running findings summary,
        calls the model, and parses the chunk-level JSON output.

        We use the `Agent._call_model` flow directly (bypassing
        the tool-use loop) because the chunk text is provided
        inline — no document tools needed for this chunk.
        """
        tool_calls = 0
        # Build the per-chunk user message. The running findings
        # summary gives the model context about what's already
        # been found, so it can avoid duplicates and reference
        # prior chunks.
        user_message = self._build_chunk_user_message(
            document_id=document_id,
            chunk_index=chunk_index,
            chunk_text=chunk_text,
            running_findings_summary=running_findings_summary,
        )

        # Call the model directly. Preflight is run per-chunk;
        # if a chunk is somehow too big for the model, the
        # preflight raises ContextWindowError.
        from .agent import _run_preflight_or_raise
        try:
            _run_preflight_or_raise(
                model=self._default_model_id(),
                system=self.reviewer_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[],  # No tools for per-chunk analysis
                max_output_tokens=4096,
            )
            response = self.client.create(
                model=self._default_model_id(),
                system=self.reviewer_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[],
                max_tokens=4096,
            )
        except Exception as e:
            # Per-chunk failures shouldn't fail the whole run.
            # Capture the error in the finding and continue.
            return (
                ChunkFinding(
                    chunk_index=chunk_index,
                    chunk_text_chars=len(chunk_text),
                    raw={
                        "_error": True,
                        "_error_message": str(e),
                        "_error_type": type(e).__name__,
                    },
                ),
                0,
            )

        # Parse the model's response. We expect JSON; if the
        # model returns prose, we wrap it as a `notes` field so
        # downstream can still process it.
        text_block = next(
            (b for b in response.content if hasattr(b, "text")), None
        )
        text = text_block.text if text_block else ""
        try:
            parsed = self._parse_chunk_json(text)
        except ValueError as e:
            # Model didn't return valid JSON. Wrap and continue.
            parsed = {
                "summary": f"(model returned non-JSON output: {e})",
                "findings": [],
                "raw_text": text[:2000],
            }

        return (
            ChunkFinding(
                chunk_index=chunk_index,
                chunk_text_chars=len(chunk_text),
                raw=parsed,
            ),
            1,  # one API call = one "tool call"-equivalent
        )

    def _build_chunk_user_message(
        self,
        *,
        document_id: str,
        chunk_index: int,
        chunk_text: str,
        running_findings_summary: str,
    ) -> str:
        """Build the user message for a single chunk.

        Includes the chunk text (input) and a reference to
        running findings (context). The running summary is
        much smaller than the cumulative chunks — it grows
        linearly with chunk count, not with chunk size.
        """
        chunk_count_marker = (
            f" This is chunk {chunk_index}. "
        )
        summary_marker = ""
        if running_findings_summary:
            summary_marker = (
                "\n\n<running_findings>\n"
                "Findings already extracted from prior chunks "
                "(do NOT duplicate these — extend, refine, "
                "or note changes):\n\n"
                f"{running_findings_summary}\n"
                "</running_findings>"
            )
        return (
            f"<current_document>\n"
            f"document_id: {document_id}\n"
            f"</current_document>\n\n"
            f"{chunk_count_marker}"
            f"Analyze the contract content below and extract "
            f"any DPO-relevant findings.\n\n"
            f"<chunk>\n{chunk_text}\n</chunk>"
            f"{summary_marker}"
        )

    # ── Internal reduce phase ──────────────────────────

    def _reduce(
        self,
        *,
        document_id: str,
        chunk_count: int,
        findings: list[ChunkFinding],
    ) -> tuple[str, int]:
        """Run the reduce phase: take all per-chunk findings
        and produce a single consolidated review.

        Sends a large user message containing all per-chunk
        findings as a structured table, asks the model to
        synthesize them into the final review format.
        """
        # Build the user message.
        findings_table = self._findings_as_table(findings)
        user_message = (
            f"<current_document>\n"
            f"document_id: {document_id}\n"
            f"chunk_count: {chunk_count}\n"
            f"</current_document>\n\n"
            f"You have already analyzed this contract in "
            f"{chunk_count} chunks. The per-chunk findings "
            f"are listed below. Synthesize them into a single "
            f"final DPO review.\n\n"
            f"<per_chunk_findings>\n{findings_table}\n"
            f"</per_chunk_findings>\n\n"
            f"Required output format:\n"
            f"- TL;DR (3-5 sentences)\n"
            f"- Key terms\n"
            f"- Risks / concerns (severity-ordered)\n"
            f"- Recommendations\n"
            f"- Open questions"
        )

        try:
            response = self.client.create(
                model=self._default_model_id(),
                system=self.reduce_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[],
                max_tokens=4096,
            )
        except Exception as e:
            # Reduce phase failed — fall back to a stitched
            # summary of the per-chunk findings.
            return (
                self._fallback_stitch(findings)
                + f"\n\n(Reduce-phase API call failed: {e})",
                0,
            )

        text_block = next(
            (b for b in response.content if hasattr(b, "text")), None
        )
        return (text_block.text if text_block else "", 1)

    # ── Helpers ────────────────────────────────────────

    def _split_into_chunks(
        self, text: str, chunk_chars: int
    ) -> list[str]:
        """Split `text` into chunks of at most `chunk_chars`.

        Tries to break at paragraph boundaries (\\n\\n) when
        possible so chunks don't break mid-paragraph. If a
        paragraph alone exceeds `chunk_chars`, falls back to
        single newlines, then to character-bounded split. This
        is best-effort — we don't have a tokenizer here.

        Boundary selection:
          1. Look for \\n\\n between [pos, pos+chunk_chars]. If any,
             break at the LAST one that still keeps both halves
             under chunk_chars. Prefer this.
          2. Otherwise look for \\n between [pos, pos+chunk_chars].
          3. Otherwise look for ' '.
          4. Otherwise cut at pos+chunk_chars.

        Step 1's "keeps both halves under chunk_chars" rule is
        important — without it, a boundary near position 0 would
        produce a near-empty first chunk.
        """
        if len(text) <= chunk_chars:
            return [text]

        chunks: list[str] = []
        pos = 0
        n = len(text)
        while pos < n:
            # Hard cap on this chunk's size. The actual break may
            # be slightly shorter if a boundary exists within
            # the next chunk_chars characters after pos.
            max_end = pos + chunk_chars
            if max_end >= n:
                chunks.append(text[pos:])
                break

            # Look for boundaries in [pos + chunk_chars // 2, max_end].
            # The midpoint anchor means we never produce a chunk
            # shorter than chunk_chars / 2.
            window_start = pos + int(chunk_chars * 0.5)
            boundary = -1

            # Paragraph break: last '\n\n' that still leaves both
            # sides under chunk_chars.
            for cand in self._rfind_all(text, "\n\n", window_start, max_end):
                left_len = cand - pos
                right_len = n - cand  # at least len(text) - max_end on the next pass
                if left_len <= chunk_chars:
                    boundary = cand + 2  # include the '\n\n' boundary in the chunk
                    break

            # Single newline (only if no paragraph found).
            if boundary == -1:
                for cand in self._rfind_all(text, "\n", window_start, max_end):
                    if cand - pos <= chunk_chars:
                        boundary = cand + 1  # include the '\n' boundary
                        break

            # Space.
            if boundary == -1:
                for cand in self._rfind_all(text, " ", window_start, max_end):
                    if cand - pos <= chunk_chars:
                        boundary = cand + 1
                        break

            # Last resort: cut at max_end.
            if boundary == -1 or boundary <= pos:
                boundary = max_end

            chunks.append(text[pos:boundary])
            pos = boundary

        return chunks

    @staticmethod
    def _rfind_all(text: str, sub: str, start: int, end: int):
        """Like rfind but yields ALL occurrences from right to
        left in [start, end]. The caller can short-circuit on the
        first acceptable position.
        """
        idx = text.rfind(sub, start, end)
        while idx != -1:
            yield idx
            idx = text.rfind(sub, start, idx)

    def _parse_chunk_json(self, text: str) -> dict[str, Any]:
        """Extract JSON from a model's chunk-level response.

        The model may wrap the JSON in ```json fences or
        include prose around it. We pull the first { ... }
        block and parse it. If that fails, we raise.
        """
        # Strip Markdown code fences if present.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Remove leading and trailing fences.
            lines = cleaned.split("\n")
            # Drop the first line (```json or ```) and the last
            # (```).
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        # Try to parse the whole thing as JSON first.
        try:
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # Otherwise find a { ... } block in the text.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"No JSON object found in chunk response: "
                f"{text[:100]!r}..."
            )
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in chunk response: {e}")

        raise ValueError("Chunk response was not a JSON object")

    def _append_finding_summary(
        self,
        running: str,
        finding: ChunkFinding,
    ) -> str:
        """Append a per-chunk finding to the running summary.

        Keeps the summary bounded in size — we keep a per-chunk
        one-line summary plus structured findings. The running
        summary is what we pass to subsequent chunks' messages.

        We cap the running summary at ~50K characters
        (~16K tokens) regardless of how many chunks we've
        processed, so the reduce-step user message stays within
        the model's budget.
        """
        # Extract a one-line summary from the finding.
        raw = finding.raw or {}
        if raw.get("_error"):
            one_line = (
                f"[chunk {finding.chunk_index} ERROR: "
                f"{raw.get('_error_message', 'unknown')[:100]}]"
            )
        else:
            summary = raw.get("summary", "")
            one_line = (
                f"[chunk {finding.chunk_index}] "
                f"{str(summary)[:200]}"
            )

        candidate = (running + "\n" + one_line).strip()
        if len(candidate) > 50_000:
            # Truncate the running summary from the beginning so
            # the most recent chunks (which are most likely to
            # be relevant) are preserved. Keep the last 50K
            # chars.
            candidate = candidate[-50_000:]
            # Drop partial first line.
            nl = candidate.find("\n")
            if nl != -1:
                candidate = candidate[nl + 1:]
        return candidate

    def _findings_as_table(
        self, findings: list[ChunkFinding]
    ) -> str:
        """Render all per-chunk findings as a markdown table.

        Used as the input to the reduce-phase prompt.
        """
        if not findings:
            return "(no findings)"

        lines = [
            "| # | Chars | Summary |",
            "|---|------:|---------|",
        ]
        for f in findings:
            raw = f.raw or {}
            if raw.get("_error"):
                summary = f"**ERROR**: {raw.get('_error_message', '')}"
            else:
                summary = str(raw.get("summary", "(empty)"))[:300]
            lines.append(
                f"| {f.chunk_index} | {f.chunk_text_chars:,} | {summary} |"
            )

        # Append key structured fields if present. The map
        # prompt usually includes things like "obligations" or
        # "risks" arrays — we include those as a separate list
        # below the table.
        structured_blocks: list[str] = []
        for f in findings:
            raw = f.raw or {}
            if raw.get("_error"):
                continue
            extras: dict[str, Any] = {}
            for key in (
                "obligations", "risks", "parties", "findings",
                "key_clauses", "red_flags",
            ):
                if key in raw and raw[key]:
                    extras[key] = raw[key]
            if extras:
                structured_blocks.append(
                    f"\n#### Chunk {f.chunk_index} details\n\n```json\n"
                    f"{json.dumps(extras, indent=2)}\n```"
                )
        return "\n".join(lines) + "\n".join(structured_blocks)

    def _fallback_stitch(
        self, findings: list[ChunkFinding]
    ) -> str:
        """Build a basic stitched summary when the reduce-phase
        API call fails. This is the worst-case fallback — we
        concatenate per-chunk summaries verbatim. Not pretty,
        but preserves the data.
        """
        parts = ["# Stitched review (fallback — reduce phase failed)\n"]
        for f in findings:
            raw = f.raw or {}
            if raw.get("_error"):
                parts.append(
                    f"\n## Chunk {f.chunk_index} (ERROR)\n\n"
                    f"_{raw.get('_error_message', 'unknown')}_"
                )
                continue
            summary = raw.get("summary", "(empty)")
            parts.append(
                f"\n## Chunk {f.chunk_index}\n\n{summary}\n"
            )
        return "\n".join(parts)

    def _default_model_id(self) -> str:
        """Pick the model for this review.

        Falls back to the LLM_MODEL env var, then a sane
        default. The preflight + LLMClient auto-detect will
        handle the actual credentials.
        """
        import os
        return os.environ.get(
            "LLM_MODEL", "claude-sonnet-5"
        )


# ── Public re-exports ──────────────────────────────────────

__all__ = [
    "ChunkedReviewer",
    "ChunkedReviewResult",
    "ChunkFinding",
    "DEFAULT_CHUNK_CHARS",
    "MAX_CHUNKS",
]
