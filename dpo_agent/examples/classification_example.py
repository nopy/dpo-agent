"""Example: clause classification against a CUAD-style taxonomy.

This example shows how to:
1. Define a taxonomy of clause types (CUAD-inspired, simplified
   to 12 categories for the demo).
2. Construct an Agent with task="clause_classification" and pass
   the taxonomy as the `schema` parameter.
3. Get the classifications back as result.review (parse with
   json.loads).

The CUAD (Contract Understanding Atticus Dataset) taxonomy has
41 categories. We use a 12-category subset for clarity. In
production, you'd pass the full taxonomy or your firm's
internal taxonomy.

For production: replace the InMemoryDocStore with a real
document store (CLM, S3, database) and write your own
DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


# A 12-category CUAD-style taxonomy. Use Format 2 (rich list
# with descriptions) so the agent can disambiguate similar
# labels.
TAXONOMY = json.dumps([
    {"label": "indemnification",
     "description": "Clauses imposing indemnification obligations on either party."},
    {"label": "limitation_of_liability",
     "description": "Clauses capping or limiting liability, including exclusions of consequential damages."},
    {"label": "termination_for_convenience",
     "description": "Clauses allowing either party to terminate the agreement without cause, with notice."},
    {"label": "termination_for_cause",
     "description": "Clauses allowing termination for material breach, with a cure period."},
    {"label": "confidentiality",
     "description": "Clauses imposing confidentiality obligations on either party, with a term and exclusions."},
    {"label": "payment_terms",
     "description": "Clauses specifying payment amounts, schedules, late-payment interest, etc."},
    {"label": "ip_ownership_assignment",
     "description": "Clauses assigning IP rights between the parties, including work-for-hire."},
    {"label": "data_protection",
     "description": "Clauses addressing personal data processing, DPA, GDPR, breach notification."},
    {"label": "non_compete",
     "description": "Clauses restricting either party's ability to compete with the other."},
    {"label": "governing_law",
     "description": "Clauses specifying which jurisdiction's law governs the agreement."},
    {"label": "minimum_commitment",
     "description": "Clauses imposing minimum purchase, minimum usage, or volume commitments."},
    {"label": "most_favored_nation",
     "description": "Clauses guaranteeing the most-favored-nation pricing or terms."},
])


def main() -> int:
    # Use a small example MSA. The agent should classify each
    # substantive clause (and skip boilerplate like notices,
    # severability, entire agreement, signatures).
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as
of 2024-03-01.

1. SERVICES

Provider shall provide the Services as described in
Schedule A.

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

8. DATA PROTECTION

Provider shall act as a Processor under GDPR Art. 28. All 10
mandatory DPA terms are included in the attached Data Processing
Addendum. Breach notification within 24 hours.

9. NOTICES

All notices under this Agreement shall be in writing and
sent to the addresses set forth above.

10. ENTIRE AGREEMENT

This Agreement, including its Schedules, constitutes the
entire agreement between the parties.

11. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware, without regard to its conflict of laws provisions.
""")
    tools = store.as_document_tools()

    # Construct the agent for the clause_classification task.
    agent = Agent(tools=tools, task="clause_classification")

    # Run with the taxonomy as the schema.
    result = agent.run(
        document_id="example-msa",
        schema=TAXONOMY,
    )

    # The output is a JSON string. Parse it.
    try:
        classification = json.loads(result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse agent output as JSON: {e}")
        print("Raw output:")
        print(result.review)
        return 1

    # Print the result.
    print("=" * 70)
    print("CLASSIFICATION RESULT")
    print("=" * 70)
    print(json.dumps(classification, indent=2, ensure_ascii=False))
    print("=" * 70)
    print(f"Tool calls: {result.tool_calls}")
    print(f"Chunks read: {result.chunks_read}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")

    # Pretty-print a summary.
    if "executive_summary" in classification:
        es = classification["executive_summary"]
        print()
        print(f"Total clauses: {es.get('total_clauses', '?')}")
        print(f"Total label assignments: {es.get('total_labels_assigned', '?')}")
        print(f"Taxonomy coverage: {es.get('taxonomy_coverage', 0):.0%}")
        print(f"Labels used: {es.get('labels_used', [])}")
        print(f"Labels not used: {es.get('labels_not_used', [])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
