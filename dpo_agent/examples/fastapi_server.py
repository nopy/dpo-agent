"""FastAPI server exposing the DPO agent as a streaming endpoint.

Three endpoints:
- POST /review         — single-task, returns the review as JSON.
- POST /review/stream  — single-task streaming SSE.
- POST /pipeline/stream — full 5-task triage pipeline with SSE.

Plus a static-file mount at / that serves the web frontend
(see dpo_agent/web/).

Install with: pip install dpo-agent[server]
Run with: uvicorn dpo_agent.examples.fastapi_server:app --reload

In production, replace `build_default_tools()` with a function that
wires the 4 document tools to your real document store.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dpo_agent import (
    Agent,
    AgentTwoPass,
    Navigator,
    PipelineConfig,
    PipelineStage,
    StreamingAgent,
    TriagePipeline,
    DocumentTools,
    list_tasks,
)
from dpo_agent.exceptions import DPOError

from dpo_agent.examples.in_memory_tools import (
    InMemoryDocStore,
    EXAMPLE_CONTRACT,
)


# --- Request / response schemas ---

class ReviewRequest(BaseModel):
    task: str = Field(default="dpo", description=f"Task to run. Available: {list_tasks()}")
    document_id: str = Field(..., description="The contract to review")
    defined_terms: dict[str, str] | None = None
    parties: list[dict[str, str]] | None = None
    governing_law_hypothesis: str | None = None
    jurisdiction_notes: str = ""
    two_pass: bool = False
    use_navigator: bool = False
    # For the example server, an inline contract body for testing.
    inline_text: str | None = Field(
        default=None,
        description="Optional inline contract text (for testing without a real store)",
    )
    # For the metadata task: a JSON schema string.
    schema_str: str | None = Field(
        default=None,
        alias="schema",
        description="JSON schema string (metadata task only)",
    )
    # Pydantic v2: keep the wire name as 'schema' (above) but
    # the Python attribute as schema_str to avoid shadowing
    # BaseModel.schema().

    model_config = {"populate_by_name": True}


# --- App factory ---

app = FastAPI(
    title="dpo-agent",
    description="Tool-using LLM agent for contract review and metadata extraction",
    version="0.2.0",
)


def build_default_tools() -> DocumentTools:
    """Build a DocumentTools instance. Override this in production
    to wire to a real document store (CLM, S3, etc.).

    The example uses an in-memory store seeded with the bundled
    example DPA contract under document_id="example-dpa".
    """
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-dpa", EXAMPLE_CONTRACT)
    return store.as_document_tools()


# Module-level state for the example. In production, use a
# dependency-injected store keyed by tenant.
_DEFAULT_TOOLS: DocumentTools | None = None


def get_tools(request_store: dict[str, str] | None = None) -> DocumentTools:
    """Get the document tools. If `request_store` is provided, the
    caller is supplying inline contracts; we build a fresh
    InMemoryDocStore for that request. Otherwise we use the
    default store (which only has the example DPA).
    """
    global _DEFAULT_TOOLS
    if request_store:
        store = InMemoryDocStore(chunk_size=4000)
        for doc_id, text in request_store.items():
            store.add(doc_id, text)
        return store.as_document_tools()
    if _DEFAULT_TOOLS is None:
        _DEFAULT_TOOLS = build_default_tools()
    return _DEFAULT_TOOLS


# --- Endpoints ---

@app.post("/review")
async def review(req: ReviewRequest) -> dict:
    """Single-pass DPO review. Returns the review text as JSON."""
    if not req.inline_text and req.document_id == "example-dpa":
        tools = get_tools()
    elif req.inline_text:
        tools = get_tools({req.document_id: req.inline_text})
    else:
        # Production: would resolve document_id against a real store.
        # For the example, we fall back to the default.
        tools = get_tools()

    try:
        # Build the kwargs shared by all three branches.
        run_kwargs = dict(
            document_id=req.document_id,
            defined_terms=req.defined_terms,
            parties=req.parties,
            governing_law_hypothesis=req.governing_law_hypothesis,
            jurisdiction_notes=req.jurisdiction_notes,
        )
        if req.schema_str:
            run_kwargs["schema"] = req.schema_str

        if req.use_navigator:
            nav = Navigator(tools=tools, task=req.task)
            nav_result = nav.navigate(**run_kwargs)
            reviewer = Agent(tools=tools, task=req.task)
            rev_result = reviewer.run(
                **run_kwargs,
                findings_packet=nav_result.packet,
                chunks_already_read=nav_result.chunks_read,
            )
            return {
                "review": rev_result.review,
                "tool_calls": nav_result.tool_calls + rev_result.tool_calls,
                "chunks_read": sorted(set(nav_result.chunks_read)),
                "elapsed_seconds": nav_result.elapsed_seconds + rev_result.elapsed_seconds,
            }

        if req.two_pass:
            agent = AgentTwoPass(tools=tools, task=req.task)
            result = agent.run(**run_kwargs)
            return {
                "review": result.pass2_review,
                "pass1_review": result.pass1_review,
                "pass1_tool_calls": result.pass1_tool_calls,
                "pass2_tool_calls": result.pass2_tool_calls,
                "chunks_read": result.chunks_read,
                "elapsed_seconds": result.elapsed_seconds,
            }

        agent = Agent(tools=tools, task=req.task)
        result = agent.run(**run_kwargs)
        return {
            "review": result.review,
            "tool_calls": result.tool_calls,
            "chunks_read": result.chunks_read,
            "elapsed_seconds": result.elapsed_seconds,
        }
    except DPOError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/review/stream")
async def review_stream(req: ReviewRequest) -> StreamingResponse:
    """Streaming DPO review. Yields SSE events as the pipeline runs."""
    if req.inline_text:
        tools = get_tools({req.document_id: req.inline_text})
    else:
        tools = get_tools()

    async def event_generator() -> AsyncIterator[str]:
        def run_sync():
            run_kwargs = dict(
                document_id=req.document_id,
                two_pass=req.two_pass,
                defined_terms=req.defined_terms,
                parties=req.parties,
                governing_law_hypothesis=req.governing_law_hypothesis,
                jurisdiction_notes=req.jurisdiction_notes,
            )
            if req.schema_str:
                run_kwargs["schema"] = req.schema_str
            return list(StreamingAgent(tools=tools, task=req.task).review_streaming(
                **run_kwargs,
            ))

        events = await asyncio.to_thread(run_sync)
        for event in events:
            payload = json.dumps({
                "type": event.type,
                "agent": event.agent,
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
                "section": event.section,
                "text": event.text,
                "error": event.error,
                "elapsed_ms": event.elapsed_ms,
            })
            yield f"data: {payload}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class PipelineRequest(BaseModel):
    """Request for the full triage pipeline."""
    document_id: str = Field(..., description="The contract to triage")
    jurisdiction_notes: str = ""
    # Inline contract body for testing without a real store.
    inline_text: str | None = Field(
        default=None,
        description="Optional inline contract text",
    )


@app.post("/pipeline/stream")
async def pipeline_stream(req: PipelineRequest) -> StreamingResponse:
    """Streaming triage pipeline. Yields SSE events as each
    of the 5 stages completes, then a final event with the
    full TriageReport.

    Event types:
    - "stage_start": a stage is starting (sent at the beginning
      of each stage)
    - "stage_complete": a stage completed (sent at the end
      of each stage, with the stage output)
    - "pipeline_complete": the full pipeline completed, with
      the TriageReport
    - "error": an error occurred in a stage
    """
    if req.inline_text:
        tools = get_tools({req.document_id: req.inline_text})
    else:
        tools = get_tools()

    # The TriagePipeline uses the on_stage_complete callback
    # to emit SSE events. We run it in a thread (the agents
    # are sync) and stream events as they come.
    import queue
    import threading

    event_queue: queue.Queue = queue.Queue()
    pipeline_done = threading.Event()
    pipeline_result: dict = {}

    def on_stage(stage: PipelineStage) -> None:
        """Called by the pipeline after each stage. Pushes
        an SSE event to the queue."""
        try:
            output = None
            if stage.result is not None and stage.succeeded:
                try:
                    output = json.loads(stage.result.review)
                except (json.JSONDecodeError, ValueError):
                    output = stage.result.review
            event_queue.put({
                "type": "stage_complete",
                "task": stage.task,
                "succeeded": stage.succeeded,
                "elapsed_seconds": stage.elapsed_seconds,
                "tool_calls": stage.tool_calls,
                "chunks_read": stage.chunks_read,
                "cost_estimate": stage.cost_estimate,
                "output": output,
                "error": stage.error,
            })
        except Exception as e:
            event_queue.put({"type": "error", "error": str(e)})

    def run_pipeline() -> None:
        try:
            pipeline = TriagePipeline(
                tools=tools,
                config=PipelineConfig(
                    auto_confirm=True,
                    on_stage_complete=on_stage,
                ),
            )
            report = pipeline.run(
                document_id=req.document_id,
                jurisdiction_notes=req.jurisdiction_notes,
            )
            pipeline_result["report"] = report
        except Exception as e:
            event_queue.put({"type": "error", "error": str(e)})
        finally:
            pipeline_done.set()

    async def event_generator() -> AsyncIterator[str]:
        # Start the pipeline in a thread.
        thread = threading.Thread(target=run_pipeline, daemon=True)
        thread.start()

        # Yield a stage_start event for each of the 5 stages
        # in the default plan. We don't know the plan until
        # the pipeline starts, so we emit these optimistically.
        from dpo_agent.pipeline import DEFAULT_TRIAGE_PLAN
        for task in DEFAULT_TRIAGE_PLAN:
            yield f"data: {json.dumps({'type': 'stage_start', 'task': task})}\n\n"
            # Small delay so the client renders them in order
            await asyncio.sleep(0.01)

        # Yield events from the queue as they come.
        while not pipeline_done.is_set() or not event_queue.empty():
            try:
                event = event_queue.get(timeout=0.1)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue

        # Drain any remaining events.
        while not event_queue.empty():
            event = event_queue.get_nowait()
            yield f"data: {json.dumps(event)}\n\n"

        # Final event with the full report.
        if "report" in pipeline_result:
            report = pipeline_result["report"]
            # Serialize the report. For very large reports, the
            # client may want to fetch it via /pipeline/report
            # instead. But for typical contracts, embedding
            # in the SSE stream is fine.
            yield f"data: {json.dumps({'type': 'pipeline_complete', 'document_id': report.document_id, 'total_elapsed_seconds': report.total_elapsed_seconds, 'total_cost_estimate': report.total_cost_estimate, 'markdown': report.markdown, 'json': report.json})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/pipeline")
async def pipeline_blocking(req: PipelineRequest) -> dict:
    """Non-streaming pipeline. Returns the full TriageReport
    as JSON. Use this when you don't need real-time
    progress (e.g. batch processing)."""
    if req.inline_text:
        tools = get_tools({req.document_id: req.inline_text})
    else:
        tools = get_tools()
    pipeline = TriagePipeline(
        tools=tools,
        config=PipelineConfig(auto_confirm=True),
    )
    report = pipeline.run(
        document_id=req.document_id,
        jurisdiction_notes=req.jurisdiction_notes,
    )
    return {
        "document_id": report.document_id,
        "total_elapsed_seconds": report.total_elapsed_seconds,
        "total_cost_estimate": report.total_cost_estimate,
        "markdown": report.markdown,
        "json": report.json,
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# Static-file mount for the web frontend. Must be last — FastAPI
# matches routes in order. The web/ directory has index.html,
# styles.css, and app.js.
_WEB_DIR = Path(__file__).parent.parent / "web"
if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
