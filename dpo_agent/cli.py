"""CLI runner for dpo-agent.

Run a single-pass, two-pass, navigator+reviewer, or streaming
extraction on a single document from the command line.

The task is selectable: `--task dpo` for contract review,
`--task metadata` for metadata extraction. Each task has its own
system prompts under dpo_agent.tasks.

For interactive / streaming use, see examples/fastapi_server.py.

Usage:
    # 1. Add a document to your store (in-memory or external).
    # 2. Set the ANTHROPIC_API_KEY environment variable.
    # 3. Run:
    dpo-review --task dpo --in-memory-example
    dpo-review --task dpo --in-memory-example --two-pass
    dpo-review --task dpo --in-memory-example --streaming
    dpo-review --task metadata --in-memory-example
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    from dpo_agent import list_tasks
    available_tasks = list_tasks()

    parser = argparse.ArgumentParser(
        description="dpo-agent: contract review and metadata extraction",
        prog="dpo-review",
    )
    parser.add_argument("--task", default="dpo", choices=available_tasks,
        help=f"Task to run (default: dpo). Available: {available_tasks}")
    parser.add_argument("--document-id", required=True,
        help="The document to review/extract")
    parser.add_argument("--two-pass", action="store_true",
        help="Run the self-refine critique pass after the first pass")
    parser.add_argument("--streaming", action="store_true",
        help="Stream progress events to stderr")
    parser.add_argument("--nav-first", action="store_true",
        help="Use the navigator+reviewer two-stage pipeline "
             "(recommended for documents > 100K chars)")
    parser.add_argument("--in-memory-example", action="store_true",
        help="Run on the bundled example DPA contract (no setup)")
    # Metadata-task-specific: pass a JSON schema as a string.
    parser.add_argument("--schema", default=None,
        help="JSON schema string (metadata task only)")
    parser.add_argument("--pipeline", action="store_true",
        help="Run the full 5-task triage pipeline "
             "(summarize → clause_classification → obligations → "
             "risk_score → dpo) instead of a single task. "
             "Outputs a unified TriageReport with both markdown "
             "and JSON.")

    args = parser.parse_args()

    # Lazy imports — only need anthropic if we actually run.
    from dpo_agent import (
        Agent,
        AgentTwoPass,
        Navigator,
        StreamingAgent,
    )
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore, EXAMPLE_CONTRACT

    store = InMemoryDocStore(chunk_size=4000)
    if args.in_memory_example:
        # For the metadata task, also seed a simple invoice example
        # if the user didn't pass their own.
        if args.task == "metadata" and args.document_id == "example-dpa":
            # Use a generic example document. Currently we only
            # have the DPA contract; metadata extraction on it
            # is fine.
            pass
        store.add(args.document_id, EXAMPLE_CONTRACT)
    else:
        print("ERROR: --in-memory-example is the only mode wired up "
              "in this CLI. Wire your own document store and call "
              "store.as_document_tools() to use a real source.",
              file=sys.stderr)
        return 2

    tools = store.as_document_tools()

    if args.pipeline:
        # Full triage pipeline: 5 tasks in sequence.
        from dpo_agent import TriagePipeline, PipelineConfig
        pipeline = TriagePipeline(
            tools=tools,
            config=PipelineConfig(auto_confirm=True),
        )
        report = pipeline.run(
            document_id=args.document_id,
            jurisdiction_notes=(
                "Provider is a US SaaS vendor; Customer has "
                "EU and US data subjects."
            ),
        )
        # Print summary to stderr.
        print(f"[pipeline] {len(report.stages)} stages, "
              f"{report.total_elapsed_seconds:.1f}s, "
              f"~${report.total_cost_estimate:.2f}",
              file=sys.stderr)
        for stage in report.stages:
            status = "ok" if stage.succeeded else "FAIL"
            print(f"  [{status}] {stage.task}: "
                  f"{stage.elapsed_seconds:.1f}s, "
                  f"{stage.tool_calls} tool calls",
                  file=sys.stderr)
        # Print the markdown report to stdout.
        print(report.markdown)
        return 0

    if args.streaming:
        agent = StreamingAgent(tools=tools, task=args.task)
        for event in agent.review_streaming(
            document_id=args.document_id,
            two_pass=args.two_pass,
        ):
            _print_event(event)
        return 0

    if args.nav_first:
        # Two-stage pipeline: navigator (Haiku) + reviewer (Sonnet).
        nav = Navigator(tools=tools, task=args.task)
        nav_kwargs = {}
        if args.schema:
            nav_kwargs["schema"] = args.schema
        nav_result = nav.navigate(
            document_id=args.document_id, **nav_kwargs,
        )
        print(f"[navigator] {nav_result.tool_calls} tool calls, "
              f"{len(nav_result.chunks_read)} chunks read, "
              f"{nav_result.elapsed_seconds:.1f}s",
              file=sys.stderr)
        reviewer = Agent(tools=tools, task=args.task)
        rev_kwargs = {
            "findings_packet": nav_result.packet,
            "chunks_already_read": nav_result.chunks_read,
        }
        if args.schema:
            rev_kwargs["schema"] = args.schema
        rev_result = reviewer.run(
            document_id=args.document_id, **rev_kwargs,
        )
        print(f"[reviewer]  {rev_result.tool_calls} tool calls, "
              f"{rev_result.elapsed_seconds:.1f}s",
              file=sys.stderr)
        _print_result(rev_result.review, args.task)
        return 0

    if args.two_pass:
        agent = AgentTwoPass(tools=tools, task=args.task)
        kwargs = {}
        if args.schema:
            kwargs["schema"] = args.schema
        result = agent.run(document_id=args.document_id, **kwargs)
        print(f"[pass 1] {result.pass1_tool_calls} tool calls",
              file=sys.stderr)
        print(f"[pass 2] {result.pass2_tool_calls} tool calls",
              file=sys.stderr)
        _print_result(result.pass2_review, args.task)
        return 0

    # Single-pass
    agent = Agent(tools=tools, task=args.task)
    kwargs = {}
    if args.schema:
        kwargs["schema"] = args.schema
    result = agent.run(document_id=args.document_id, **kwargs)
    print(f"[review] {result.tool_calls} tool calls, "
          f"{len(result.chunks_read)} chunks read, "
          f"{result.elapsed_seconds:.1f}s",
          file=sys.stderr)
    _print_result(result.review, args.task)
    return 0


def _print_result(text: str, task: str) -> None:
    """Format the agent output for stdout.

    For the metadata task, the output is a JSON object — try to
    pretty-print it. For other tasks, print the text as-is.
    """
    if task == "metadata":
        try:
            parsed = json.loads(text)
            print(json.dumps(parsed, indent=2, ensure_ascii=False))
            return
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to plain text
    print(text)


def _print_event(event) -> None:
    """Format an AgentEvent for stderr output."""
    if event.type == "agent_start":
        print(f"[{event.agent}] starting...", file=sys.stderr)
    elif event.type == "tool_call_start":
        idx = (event.tool_input or {}).get("index", "?")
        if event.tool_name == "get_document_chunk_by_index":
            print(f"  [{event.agent}] reading chunk {idx}",
                  file=sys.stderr)
        else:
            print(f"  [{event.agent}] {event.tool_name}({event.tool_input})",
                  file=sys.stderr)
    elif event.type == "tool_call_complete":
        if event.error:
            print(f"  [{event.agent}] ERROR: {event.error}",
                  file=sys.stderr)
    elif event.type == "section_complete":
        print(f"  [{event.agent}] section: {event.section}",
              file=sys.stderr)
    elif event.type == "agent_complete":
        elapsed = event.elapsed_ms / 1000
        print(f"[{event.agent}] done in {elapsed:.1f}s "
              f"(section={event.section})", file=sys.stderr)
    elif event.type == "agent_error":
        print(f"[{event.agent}] FAILED: {event.error}",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
