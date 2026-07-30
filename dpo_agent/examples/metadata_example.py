"""Example: metadata extraction from a document with the Agent.

This example shows how to:
1. Define a JSON schema for the fields you want to extract.
2. Construct an Agent with task="metadata" and pass the schema
   as a string.
3. Get the JSON extraction back as result.review (parse with
   json.loads).

For production: replace the InMemoryDocStore with a real
document store (CLM, S3, database) and write your own
DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


# The schema describes what we want to extract. It's a JSON
# string passed to the agent; the agent uses it to structure
# its output. You can use any schema format the LLM understands
# (JSON Schema, TypeScript-style types, even a natural-language
# description).
SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "parties": {
            "type": "array",
            "description": "All parties to the contract, in order of appearance",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Full legal entity name"},
                    "role": {"type": "string",
                             "description": "Role: Provider, Customer, "
                                            "Indemnitor, etc."},
                },
            },
        },
        "effective_date": {
            "type": "string",
            "description": "Effective date in ISO 8601 (YYYY-MM-DD)",
        },
        "term_months": {
            "type": "integer",
            "description": "Contract term in months, or null if perpetual",
        },
        "governing_law": {
            "type": "string",
            "description": "Governing law jurisdiction (e.g. 'Delaware, USA')",
        },
        "payment_terms": {
            "type": "string",
            "description": "Payment terms summary (e.g. 'Net 30')",
        },
        "termination_for_convenience": {
            "type": "boolean",
            "description": "True if either party can terminate "
                           "without cause",
        },
        "auto_renewal": {
            "type": "boolean",
            "description": "True if the contract auto-renews",
        },
    },
    "required": ["parties", "effective_date", "governing_law"],
})


def main() -> int:
    # Use the bundled example DPA contract.
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-dpa", """
DATA PROCESSING ADDENDUM

This Data Processing Addendum ("DPA") forms part of the Master
Services Agreement between Acme Corp ("Provider") and Widget Inc
("Customer") dated 2024-01-15 (the "Agreement"). The initial term
is 36 months, automatically renewing for successive 12-month
periods unless either party gives 90 days' written notice.
Either party may terminate this DPA for convenience with 30
days' written notice. Payment terms are Net 30. This DPA is
governed by the laws of the State of Delaware.
""")
    tools = store.as_document_tools()

    # Construct the agent for the metadata task.
    agent = Agent(tools=tools, task="metadata")

    # Run with the schema.
    result = agent.run(
        document_id="example-dpa",
        schema=SCHEMA,
    )

    # The output is a JSON string. Parse it.
    try:
        metadata = json.loads(result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse agent output as JSON: {e}")
        print("Raw output:")
        print(result.review)
        return 1

    # Print the result.
    print("=" * 60)
    print("EXTRACTED METADATA")
    print("=" * 60)
    print(json.dumps(metadata, indent=2, ensure_ascii=False))
    print("=" * 60)
    print(f"Tool calls: {result.tool_calls}")
    print(f"Chunks read: {result.chunks_read}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
