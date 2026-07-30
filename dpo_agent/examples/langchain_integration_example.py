"""Example: integrate dpo-agent with a deep agent.

This example shows the simplest integration: each dpo-agent
task becomes a LangChain tool, and a deep agent picks which
to call based on the user's request.

Run with: ANTHROPIC_API_KEY=... python -m dpo_agent.examples.langchain_integration_example

The example uses a fake Anthropic key check — it will print
the deep agent's tool-selection reasoning without actually
calling the LLM. To run end-to-end, set the API key and
uncomment the agent.invoke() call.

This is the FLAT integration (option 1 in the design notes):
9 tools, each is a full dpo-agent run. The deep agent picks
which to call.

For the hybrid option (triage pipeline as a single tool + a
redline subagent), see dpo_agent.pipeline.TriagePipeline +
dpo_agent.integrations.langchain.make_triage_tool.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Build the document tools (in-memory for the example).
    from dpo_agent import DocumentTools
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    from dpo_agent.examples.in_memory_tools import EXAMPLE_CONTRACT

    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", EXAMPLE_CONTRACT)
    document_tools = DocumentTools(
        get_document_size=store.size,
        retrieve_whole_document_content=store.get,
        get_number_of_chunks=store.chunk_count,
        get_document_chunk_by_index=store.get_chunk,
    )

    # Build the 9 dpo-agent tools.
    try:
        from dpo_agent.integrations.langchain import (
            make_dpo_tools,
            make_triage_tool,
        )
    except ImportError as e:
        print(f"ERROR: {e}")
        print("Install with: pip install langchain")
        return 1

    dpo_tools = make_dpo_tools(document_tools=document_tools)
    triage_tool = make_triage_tool(document_tools=document_tools)

    print(f"Built {len(dpo_tools)} dpo-agent tools + 1 triage tool:")
    for t in dpo_tools:
        print(f"  - {t.name}")
    print(f"  - {triage_tool.name}")
    print()

    # Try to construct a deep agent.
    try:
        from deepagents import create_deep_agent
    except ImportError:
        print("To run the deep agent end-to-end, install deepagents:")
        print("  pip install deepagents")
        print()
        print("The tools are ready. A deep agent would call them based")
        print("on the user's request, e.g.:")
        print()
        print('  user: "Summarize this contract"')
        print("  agent: calls summarize()")
        print('  user: "What are the risks?"')
        print("  agent: calls risk_score() and dpo()")
        print('  user: "Propose redlines against our playbook"')
        print("  agent: calls redline_suggest()")
        print('  user: "Run full triage"')
        print("  agent: calls triage_contract()")
        return 0

    # If deepagents is installed, set up the agent.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY to run the deep agent end-to-end.")
        print("The tools and agent are ready; we just can't make the LLM call.")
        return 0

    agent = create_deep_agent(
        model="anthropic:claude-sonnet-5",
        tools=dpo_tools + [triage_tool],
        system_prompt=(
            "You are a contract review assistant. You have access to "
            "10 tools: summarize, clause_classification, obligations, "
            "metadata, risk_score, dpo, redline_suggest, redline_apply, "
            "redline_negotiation, and triage_contract (the full pipeline). "
            "Use them to triage contracts, extract metadata, score risk, "
            "and produce redlines. For full intake triage, prefer "
            "triage_contract. For specific analyses, use the individual "
            "tools."
        ),
    )

    # Run on the example contract.
    result = agent.invoke({
        "messages": [{
            "role": "user",
            "content": (
                "Triage the contract at document_id='example-msa' and "
                "give me an executive summary plus the top 3 risks."
            ),
        }]
    })
    print("Agent output:")
    print(result.get("messages", [{}])[-1].get("content", "(no output)"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
