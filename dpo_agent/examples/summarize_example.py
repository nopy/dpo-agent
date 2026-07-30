"""Example: executive summary of a long document.

This example shows how to:
1. Construct an Agent with task="summarize".
2. Optionally pass audience, target_length, focus_areas, and
   document_type_hint as context.
3. Get the markdown summary back as result.review (parse
   the markdown by section).

For production: replace the InMemoryDocStore with a real
document store (CLM, S3, database) and write your own
DocumentTools.
"""

from __future__ import annotations

import re

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


def main() -> int:
    # Use a small but realistic MSA. The agent should produce
    # a 4-section summary + the optional Parties-and-Term
    # section (since the document_type_hint is "contract").
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider"), a Delaware corporation, and
Widget Inc ("Customer"), a New York corporation, as of
2024-03-01 (the "Effective Date").

1. SERVICES

Provider shall provide the Services described in Schedule A.

2. PAYMENT TERMS

Customer shall pay all invoices within 30 days of receipt.
Late payments accrue interest at 1.5% per month or the maximum
legal rate, whichever is lower.

3. CONFIDENTIALITY

Each party shall maintain the confidentiality of the other
party's confidential information for 5 years following
termination of this Agreement. Information that is (a) public,
(b) already known, (c) independently developed, or (d)
rightfully received from a third party is excluded.

4. IP OWNERSHIP

All intellectual property created by Provider in the course of
performing the Services shall be owned by Customer upon full
payment. Provider retains rights to its pre-existing
intellectual property.

5. INDEMNIFICATION

Provider shall indemnify Customer against third-party claims
arising from Provider's gross negligence or willful misconduct,
capped at 1x annual fees paid by Customer, excluding IP
infringement and breach of confidentiality.

6. LIMITATION OF LIABILITY

Provider's total liability under this Agreement shall be
capped at 2x annual fees paid in the 12 months preceding the
claim. Consequential damages are excluded.

7. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice and a 30-day cure period. Either
party may terminate for convenience with 90 days' written
notice.

8. AUTO-RENEWAL

The initial term is 36 months. The Agreement automatically
renews for successive 12-month periods unless either party
gives 90 days' written notice of non-renewal.

9. DATA PROTECTION

Provider shall act as a Processor under GDPR Art. 28. All 10
mandatory DPA terms are included in the attached Data
Processing Addendum. Breach notification within 24 hours.

10. NOTICES

All notices under this Agreement shall be in writing and
sent to the addresses set forth above.

11. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware, without regard to its conflict of laws provisions.
""")
    tools = store.as_document_tools()

    # Construct the agent for the summarize task.
    agent = Agent(tools=tools, task="summarize")

    # Run with optional context hints.
    result = agent.run(
        document_id="example-msa",
        # The audience tells the agent to emphasize DPO-relevant
        # concerns (data protection, governing law, IP).
        jurisdiction_notes=(
            "Audience: a procurement officer at a mid-size SaaS "
            "company. Focus: data protection, termination rights, "
            "payment terms, IP ownership, governing law. "
            "Target length: 500 words."
        ),
    )

    # The output is markdown, not JSON. Print it directly.
    print("=" * 70)
    print("EXECUTIVE SUMMARY")
    print("=" * 70)
    print(result.review)
    print("=" * 70)
    print(f"Tool calls: {result.tool_calls}")
    print(f"Chunks read: {result.chunks_read}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")

    # Verify the summary has all expected sections.
    expected_sections = ["## TL;DR", "## Key Terms",
                         "## Risks / Concerns", "## Open Questions",
                         "## Parties and Term"]
    print()
    print("Section check:")
    for section in expected_sections:
        present = section in result.review
        marker = "✓" if present else "✗"
        print(f"  {marker} {section}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
