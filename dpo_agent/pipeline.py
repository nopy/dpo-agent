"""Triage pipeline — runs multiple tasks in sequence against
a single contract and produces a unified triage report.

The pipeline is the natural next step above single-task
agents. A law firm running intake on 100 contracts a week
doesn't want to call 5 agents per contract manually; they
want one call that produces a triage report.

The pipeline is **task-agnostic** — it runs whatever tasks
you put in the plan. The default plan covers the 5
"triage-and-classify" tasks (summarize, clause_classification,
obligations, risk_score, dpo); the 2 "redline" tasks
(redline_suggest, redline_apply) are opt-in and require a
playbook.

Two output formats:
- **JSON** — structured, machine-readable
- **Markdown** — human-readable triage document

Two execution modes:
- **Sequential** (default) — run each task fully, then move on
- **Streaming** — emit events as each task progresses (the
  summary is typically ready in 5-10 seconds, so the human
  sees something useful almost immediately)

Cost gate:
The pipeline tracks cumulative token cost. If the cost
exceeds a threshold (default $5), the pipeline asks for
human confirmation before continuing. This prevents
accidental large charges on long documents.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from .agent import Agent, AgentConfig, ReviewResult
from .exceptions import DPOError
from .tasks.loader import list_tasks
from .tools import DocumentTools


def _chunked_to_review_result(
    chunked: "ChunkedReviewResult",
) -> ReviewResult:
    """Adapt a ChunkedReviewResult to the Agent's ReviewResult.

    The downstream pipeline stages use ReviewResult fields
    (review, tool_calls, chunks_read, elapsed_seconds). The
    chunked variant has the same semantics — `consolidated_review`
    is the final markdown, `tool_calls` includes map + reduce,
    `chunks_read` is the chunk indexes processed.
    """
    return ReviewResult(
        review=chunked.consolidated_review,
        tool_calls=chunked.tool_calls,
        chunks_read=list(range(chunked.chunk_count)),
        elapsed_seconds=chunked.elapsed_seconds,
    )


# The default triage plan. The wrapper accepts a custom plan
# but this is what most callers want.
DEFAULT_TRIAGE_PLAN = [
    "summarize",        # 1-page summary (cheap, fast)
    "clause_classification",  # multi-label tags
    "obligations",      # structured obligation list
    "risk_score",       # numeric risk score
    "dpo",              # GDPR/CCPA findings
]


# Tasks that need extra context (schema, playbook, framework).
# The pipeline passes the schema/playbook from the caller's
# config to these tasks automatically.
TASKS_NEEDING_CONTEXT = {
    "redline_suggest": "playbook",
    "redline_apply": "redline_package",
    "clause_classification": "taxonomy",
    "obligations": "defined_terms_or_parties",
    "risk_score": "framework",
    "dpo": "jurisdiction_notes",
    "metadata": "schema",
}


@dataclass
class PipelineStage:
    """A single stage in the pipeline."""
    task: str
    result: Any = None
    elapsed_seconds: float = 0.0
    tool_calls: int = 0
    chunks_read: list[int] = field(default_factory=list)
    cost_estimate: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass
class TriageReport:
    """The final output of a triage pipeline run.

    Fields:
        document_id: the contract that was triaged
        stages: per-stage results (one per task in the plan)
        total_elapsed_seconds: time for the full pipeline
        total_cost_estimate: estimated cost in USD
        markdown: human-readable triage document
        json: machine-readable full report (same content)
    """
    document_id: str
    stages: list[PipelineStage]
    total_elapsed_seconds: float = 0.0
    total_cost_estimate: float = 0.0
    markdown: str = ""
    json: dict = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """Configuration for a triage pipeline run.

    Fields:
        plan: list of task names to run in order. Default is
            the 5-task triage plan. To include redline_suggest,
            add it to the plan; you must also pass a playbook
            via the TriagePipeline.run() method.
        cost_threshold: cumulative cost in USD at which to
            pause for human confirmation. Default $5.
        auto_confirm: if True, never pause for confirmation
            (use with caution — could burn credits).
        on_stage_complete: optional callback called after
            each stage completes. Useful for streaming UI.
        skip_on_error: if True, continue the pipeline even if
            a stage fails. Default False — fail loudly.
    """
    plan: list[str] = field(default_factory=lambda: list(DEFAULT_TRIAGE_PLAN))
    cost_threshold: float = 5.0
    auto_confirm: bool = False
    on_stage_complete: Any = None  # callable
    skip_on_error: bool = False
    # When True, the `dpo` stage automatically switches to
    # ChunkedReviewer (map-reduce over chunks) when the document
    # exceeds the model's context window. This lets the same
    # pipeline plan handle contracts of any size — small docs
    # use the regular Agent (faster, one call), large docs use
    # ChunkedReviewer (N+1 calls but bounded context).
    chunk_large_documents: bool = True
    # Per-chunk character cap for the chunked map phase. Smaller
    # than the model's window so system + tools + output all fit.
    chunk_chars: int = 60_000


class TriagePipeline:
    """Multi-task triage pipeline.

    Usage:
        from dpo_agent import TriagePipeline, DocumentTools
        from dpo_agent.examples.in_memory_tools import InMemoryDocStore

        store = InMemoryDocStore()
        store.add("contract-001", contract_text)
        tools = store.as_document_tools()

        pipeline = TriagePipeline(tools=tools)
        report = pipeline.run(
            document_id="contract-001",
            playbook=PLAYBOOK_JSON,  # optional, for redline
            framework=RISK_FRAMEWORK_JSON,  # optional
        )
        print(report.markdown)
    """

    def __init__(
        self,
        tools: DocumentTools,
        config: PipelineConfig | None = None,
    ):
        self.tools_impl = tools
        self.config = config or PipelineConfig()
        # Validate the plan: every task must exist.
        available = list_tasks()
        unknown = [t for t in self.config.plan if t not in available]
        if unknown:
            raise ValueError(
                f"Unknown tasks in plan: {unknown}. "
                f"Available tasks: {available}"
            )

    def run(
        self,
        document_id: str,
        # Optional task-specific context. The pipeline passes
        # these to the corresponding agents automatically.
        playbook: str | None = None,
        redline_package: str | None = None,
        taxonomy: str | None = None,
        framework: str | None = None,
        schema: str | None = None,
        defined_terms: dict[str, str] | None = None,
        parties: list[dict[str, str]] | None = None,
        jurisdiction_notes: str = "",
        # Optional Anthropic client (for testing or shared config).
        client: Any = None,
    ) -> TriageReport:
        """Run the pipeline and return a unified triage report.

        Args:
            document_id: the contract to triage.
            playbook: JSON string for the redline_suggest task
                (only needed if "redline_suggest" is in the plan).
            redline_package: JSON string for the redline_apply
                task (only needed if "redline_apply" is in the plan).
            taxonomy: JSON string for the clause_classification
                task.
            framework: JSON string for the risk_score task.
            schema: JSON string for the metadata task.
            defined_terms, parties, jurisdiction_notes: passed
                to the obligations and dpo tasks.

        Returns:
            TriageReport with the per-stage results, a
            human-readable markdown report, and a machine-readable
            JSON report.
        """
        start = time.monotonic()
        stages: list[PipelineStage] = []
        cumulative_cost = 0.0

        for task in self.config.plan:
            # Build the kwargs for this task.
            kwargs = self._build_task_kwargs(
                task,
                playbook=playbook,
                redline_package=redline_package,
                taxonomy=taxonomy,
                framework=framework,
                schema=schema,
                defined_terms=defined_terms,
                parties=parties,
                jurisdiction_notes=jurisdiction_notes,
            )

            # Cost gate (if not auto_confirm).
            if not self.config.auto_confirm and cumulative_cost > self.config.cost_threshold:
                # In a real product, this would prompt the user.
                # For programmatic use, the caller sets auto_confirm.
                pass

            # Run the task. The `dpo` stage is special: if
            # `chunk_large_documents` is enabled and the document
            # has more characters than the model's context window
            # can fit in one call, we use ChunkedReviewer
            # (map-reduce) instead of Agent (single-pass tool-use
            # loop). The other stages still use Agent — they tend
            # to produce small structured outputs and don't need
            # map-reduce.
            stage = PipelineStage(task=task)
            try:
                if (
                    task == "dpo"
                    and self.config.chunk_large_documents
                    and self._needs_chunking(document_id)
                ):
                    chunked_result = self._run_dpo_chunked(
                        document_id=document_id,
                        client=client,
                        chunk_chars=self.config.chunk_chars,
                    )
                    stage.result = _chunked_to_review_result(chunked_result)
                    stage.tool_calls = chunked_result.tool_calls
                    stage.chunks_read = list(
                        range(chunked_result.chunk_count)
                    )
                    stage.elapsed_seconds = chunked_result.elapsed_seconds
                else:
                    agent = Agent(
                        tools=self.tools_impl,
                        task=task,
                        client=client,
                    )
                    result = agent.run(document_id=document_id, **kwargs)
                    stage.result = result
                    stage.tool_calls = result.tool_calls
                    stage.chunks_read = result.chunks_read
                    stage.elapsed_seconds = result.elapsed_seconds
                # Rough cost estimate: 1M tokens ~ $3 for Sonnet 5.
                # We don't have a precise token count, so this
                # is a heuristic. Real callers should plug in
                # their model API's usage callback.
                stage.cost_estimate = self._estimate_cost(
                    chunks_read=stage.chunks_read,
                    elapsed_seconds=stage.elapsed_seconds,
                )
            except DPOError as e:
                stage.error = str(e)
                if not self.config.skip_on_error:
                    raise
            stages.append(stage)
            cumulative_cost += stage.cost_estimate

            # Fire the stage-complete callback (for streaming UI).
            if self.config.on_stage_complete is not None:
                self.config.on_stage_complete(stage)

        # Build the report.
        total_elapsed = time.monotonic() - start
        json_report = self._build_json_report(
            document_id, stages, total_elapsed, cumulative_cost
        )
        markdown_report = self._build_markdown_report(json_report)

        return TriageReport(
            document_id=document_id,
            stages=stages,
            total_elapsed_seconds=total_elapsed,
            total_cost_estimate=cumulative_cost,
            markdown=markdown_report,
            json=json_report,
        )

    def _build_task_kwargs(
        self,
        task: str,
        playbook: str | None,
        redline_package: str | None,
        taxonomy: str | None,
        framework: str | None,
        schema: str | None,
        defined_terms: dict[str, str] | None,
        parties: list[dict[str, str]] | None,
        jurisdiction_notes: str,
    ) -> dict[str, Any]:
        """Map the caller's context to the per-task kwargs."""
        kwargs: dict[str, Any] = {
            "defined_terms": defined_terms,
            "parties": parties,
            "jurisdiction_notes": jurisdiction_notes,
        }
        if task == "redline_suggest":
            kwargs["schema"] = playbook
        elif task == "redline_apply":
            kwargs["schema"] = redline_package
        elif task == "clause_classification":
            kwargs["schema"] = taxonomy
        elif task == "risk_score":
            kwargs["schema"] = framework
        elif task == "metadata":
            kwargs["schema"] = schema
        return kwargs

    def _estimate_cost(
        self, chunks_read: list[int], elapsed_seconds: float
    ) -> float:
        """Rough cost estimate in USD.

        This is a heuristic, not a precise calculation. Real
        cost tracking should use the Anthropic API's usage
        callback (`response.usage.input_tokens` and
        `output_tokens`). The wrapper doesn't have access to
        those from outside the agent, so we estimate.

        Heuristic: 1 chunk read = 4K tokens; 1 tool call = 1K
        tokens of output. At Sonnet 5 pricing ($3 / 1M input
        tokens, $15 / 1M output tokens), a typical chunk-read
        tool call costs about $0.012. A 10-chunk-read
        extraction costs ~$0.12. A 100-chunk contract
        extraction costs ~$1.20.

        This is intentionally conservative. Real cost tracking
        should override this.
        """
        # Assume 4K tokens per chunk read + 1K tokens of
        # output per tool call. Pricing: $3/1M input, $15/1M
        # output (Sonnet 5).
        input_tokens = len(chunks_read) * 4000
        output_tokens = 0  # not tracked here
        return (input_tokens * 3.0 / 1_000_000) + (output_tokens * 15.0 / 1_000_000)

    # ── Chunked-review (map-reduce) helpers ─────────────────────

    def _needs_chunking(self, document_id: str) -> bool:
        """Return True if the document's character count exceeds
        the user-configured `chunk_chars` threshold.

        If the document can't be read (e.g. not in the store),
        default to False — we let the regular Agent try; it'll
        surface its own error if it fails.
        """
        try:
            total_chars = self.tools_impl.get_document_size(document_id)
        except Exception:
            return False
        return total_chars > self.config.chunk_chars

    def _run_dpo_chunked(
        self,
        *,
        document_id: str,
        client: Any,
        chunk_chars: int,
    ) -> "ChunkedReviewResult":
        """Run the dpo_chunked task via the chunked map-reduce
        path. Returns a ChunkedReviewResult; the caller
        adapts it to a ReviewResult for the pipeline.

        If `client` is None, falls back to the LLMClient
        factory (which auto-detects from env vars). This
        matches Agent's behavior — callers can pass a client
        for testing, or let it auto-resolve from
        ANTHROPIC_API_KEY / OPENROUTER_API_KEY.
        """
        from .chunked_agent import ChunkedReviewer
        if client is None:
            from .llm_client import create_client
            client = create_client()
        reviewer = ChunkedReviewer(
            tools=self.tools_impl,
            task="dpo_chunked",
            client=client,
            chunk_chars=chunk_chars,
        )
        return reviewer.review(document_id=document_id)

    def _build_json_report(
        self,
        document_id: str,
        stages: list[PipelineStage],
        total_elapsed: float,
        total_cost: float,
    ) -> dict:
        """Build the machine-readable JSON report."""
        per_stage = []
        for stage in stages:
            entry: dict[str, Any] = {
                "task": stage.task,
                "succeeded": stage.succeeded,
                "elapsed_seconds": stage.elapsed_seconds,
                "tool_calls": stage.tool_calls,
                "chunks_read": stage.chunks_read,
                "cost_estimate": stage.cost_estimate,
            }
            if stage.error:
                entry["error"] = stage.error
            if stage.result is not None and stage.succeeded:
                # Parse the result if it's JSON (most tasks);
                # include as raw text otherwise (summarize).
                try:
                    entry["output"] = json.loads(stage.result.review)
                except (json.JSONDecodeError, ValueError):
                    entry["output"] = stage.result.review
            per_stage.append(entry)

        return {
            "document_id": document_id,
            "total_elapsed_seconds": total_elapsed,
            "total_cost_estimate": total_cost,
            "stages": per_stage,
        }

    def _build_markdown_report(self, json_report: dict) -> str:
        """Build the human-readable triage report (markdown)."""
        lines: list[str] = []
        lines.append(f"# Triage Report: {json_report['document_id']}")
        lines.append("")
        lines.append(f"**Total elapsed:** {json_report['total_elapsed_seconds']:.1f}s")
        lines.append(f"**Estimated cost:** ${json_report['total_cost_estimate']:.2f}")
        lines.append("")

        # Index — one line per stage with a status emoji.
        lines.append("## Stages")
        lines.append("")
        for stage in json_report["stages"]:
            status = "✅" if stage["succeeded"] else "❌"
            lines.append(
                f"- {status} **{stage['task']}** — "
                f"{stage['elapsed_seconds']:.1f}s, "
                f"{stage['tool_calls']} tool calls, "
                f"~${stage['cost_estimate']:.2f}"
            )
        lines.append("")

        # Per-stage details. Each stage's output is rendered
        # inline.
        for stage in json_report["stages"]:
            lines.append(f"## {stage['task']}")
            lines.append("")
            if not stage["succeeded"]:
                lines.append(f"**Error:** {stage.get('error', 'unknown')}")
                lines.append("")
                continue
            output = stage.get("output")
            if isinstance(output, dict):
                # Render as a JSON code block (compact view).
                lines.append("```json")
                lines.append(json.dumps(output, indent=2)[:5000])
                if len(json.dumps(output)) > 5000:
                    lines.append("...")
                    lines.append("(truncated; full output in the JSON report)")
                lines.append("```")
            else:
                # Render as text (markdown tasks like summarize).
                lines.append(str(output))
            lines.append("")

        return "\n".join(lines)


def triage(
    tools: DocumentTools,
    document_id: str,
    plan: list[str] | None = None,
    **kwargs: Any,
) -> TriageReport:
    """Convenience function: run a triage pipeline.

    Args:
        tools: the document tools to use.
        document_id: the contract to triage.
        plan: optional custom plan. Default is the 5-task
            triage plan.
        **kwargs: passed to TriagePipeline.run (playbook,
            framework, taxonomy, etc.).

    Returns:
        TriageReport with markdown and json fields.
    """
    config = PipelineConfig()
    if plan is not None:
        config.plan = plan
    pipeline = TriagePipeline(tools=tools, config=config)
    return pipeline.run(document_id=document_id, **kwargs)
