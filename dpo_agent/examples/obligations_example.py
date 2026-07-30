"""Example: detect obligations in a contract.

This example shows how to:
1. Construct an Agent with task="obligations".
2. Pass defined_terms, parties, and jurisdiction_notes
   as context.
3. Get the obligation list back as result.review (parse
   with json.loads).

The output uses the canonical 5-field obligation schema
(obligor / obligee / action / deadline / condition) plus
4 optional fields (severity, recurring, monetary_amount,
currency). The 12-category obligation type taxonomy is
defined in the reviewer prompt.

For production: replace the InMemoryDocStore with a real
document store and write your own DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


def main() -> int:
    # Use a small but realistic MSA. The agent should
    # decompose each clause into individual obligations and
    # output the structured list.
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as
of 2024-03-01.

1. SERVICES

Provider shall provide the Services described in Schedule A
in accordance with the service levels specified therein.

2. PAYMENT TERMS

Customer shall pay all invoices within 30 days of receipt.
Late payments accrue interest at 1.5% per month or the
maximum legal rate, whichever is lower.

3. CONFIDENTIALITY

Each party shall maintain the confidentiality of the other
party's confidential information for 5 years following
termination of this Agreement.

4. IP OWNERSHIP

All intellectual property created by Provider in the course
of performing the Services shall be owned by Customer upon
full payment. Provider retains rights to its pre-existing
intellectual property.

5. INDEMNIFICATION

Provider shall indemnify Customer against third-party claims
arising from Provider's gross negligence or willful
misconduct, capped at 1x annual fees paid by Customer,
excluding IP infringement and breach of confidentiality.
Customer shall indemnify Provider against any third-party
claim arising from Customer's use of the Services.

6. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. Either party may terminate for
convenience with 90 days' written notice.

7. AUTO-RENEWAL

The initial term is 36 months. The Agreement automatically
renews for successive 12-month periods unless either party
gives 90 days' written notice of non-renewal.

8. DATA PROTECTION

Provider shall act as a Processor under GDPR Art. 28. All 10
mandatory DPA terms are included in the attached Data
Processing Addendum. Breach notification within 24 hours.

9. NOTICES

All notices under this Agreement shall be in writing and
sent to the addresses set forth above.

10. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware.
""")
    tools = store.as_document_tools()

    # Construct the agent for the obligations task.
    agent = Agent(tools=tools, task="obligations")

    # Run with optional context.
    result = agent.run(
        document_id="example-msa",
        # Pass defined terms so the agent uses them verbatim
        # in obligor/obligee fields.
        defined_terms={
            "Provider": "Acme Corp",
            "Customer": "Widget Inc",
        },
        parties=[
            {"name": "Acme Corp", "role": "Provider"},
            {"name": "Widget Inc", "role": "Customer"},
        ],
        jurisdiction_notes=(
            "Provider is a US SaaS vendor; Customer has EU "
            "and US data subjects. Cross-border transfer "
            "obligations are critical."
        ),
    )

    # The output is a JSON string. Parse it.
    try:
        extraction = json.loads(result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse agent output as JSON: {e}")
        print("Raw output:")
        print(result.review)
        return 1

    # Print the result.
    print("=" * 70)
    print("OBLIGATIONS EXTRACTION")
    print("=" * 70)
    print(json.dumps(extraction, indent=2, ensure_ascii=False))
    print("=" * 70)
    print(f"Tool calls: {result.tool_calls}")
    print(f"Chunks read: {result.chunks_read}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")

    # Pretty-print a summary.
    if "executive_summary" in extraction:
        es = extraction["executive_summary"]
        print()
        print(f"Total obligations: {es.get('total_obligations', '?')}")
        if "by_type" in es:
            print(f"By type: {es['by_type']}")
        if "by_severity" in es:
            print(f"By severity: {es['by_severity']}")
        if "by_obligor" in es:
            print(f"By obligor: {es['by_obligor']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
