"""Example: full redline pipeline — suggest then apply.

This example shows the full two-stage pipeline:
1. Run redline_suggest to propose redlines for a contract
   against a playbook.
2. Run redline_apply to actually substitute the proposed_text
   for current_text, producing a redlined document.

In production, these two stages are often chained:
- The first agent proposes
- A human counsel reviews the redline package
- The second agent applies the human-approved redlines
- The redlined document is sent to the counterparty

This example runs both stages automatically (no human in the
loop) to demonstrate the pipeline. For production, add a
human-review step between the two agents.

For production: replace the InMemoryDocStore with a real
document store and write your own DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore, EXAMPLE_CONTRACT


# A small but realistic playbook.
PLAYBOOK = json.dumps({
    "indemnification": {
        "preferred_language": "Mutual indemnification capped at 1x annual fees, excluding IP infringement and breach of confidentiality. Notice period: 30 days.",
        "fallback_language": "Mutual indemnification capped at 2x annual fees, with carve-outs for IP, confidentiality, and gross negligence. Notice period: 60 days.",
        "red_flags": ["uncapped indemnification", "no cap", "sole remedy is indemnification"],
        "negotiable": ["cap multiplier (1x to 2x)", "carve-out list", "notice period (30 to 90 days)"],
    },
    "limitation_of_liability": {
        "preferred_language": "Total liability capped at 2x annual fees paid in the 12 months preceding the claim. Excludes: indemnification obligations, breach of confidentiality, gross negligence, willful misconduct.",
        "fallback_language": "Total liability capped at 1x annual fees, with the same exclusions.",
        "red_flags": ["uncapped liability", "no limitation", "consequential damages not excluded"],
        "negotiable": ["cap multiplier (1x to 3x)", "exclusion list", "carve-out for IP indemnification"],
    },
    "payment_terms": {
        "preferred_language": "Net 30 days from invoice date. Late payments accrue interest at 1.5% per month or the maximum legal rate, whichever is lower.",
        "fallback_language": "Net 45 days from invoice date. Late payments accrue interest at 1.0% per month.",
        "red_flags": ["Net 90 or longer", "no late-payment interest"],
        "negotiable": ["payment window (30 to 60 days)", "interest rate (1.0% to 1.5%)"],
    },
    "termination": {
        "preferred_language": "Either party may terminate for material breach with 30 days' cure period. Either party may terminate for convenience with 90 days' written notice.",
        "fallback_language": "Either party may terminate for material breach with 60 days' cure period. Termination for convenience with 60 days' written notice.",
        "red_flags": ["no termination for convenience", "no cure period", "termination only for cause"],
        "negotiable": ["cure period (30 to 60 days)", "notice period (60 to 90 days)"],
    },
})


def main() -> int:
    # Use a small example MSA that intentionally deviates from
    # the playbook in several places.
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as
of 2024-03-01.

1. SERVICES

Provider shall provide the Services as described in Schedule A.

2. PAYMENT TERMS

Customer shall pay all invoices within 60 days of receipt.
Late payments accrue interest at 1.5% per month or the maximum
legal rate, whichever is lower.

3. INDEMNIFICATION

Provider shall indemnify Customer against any and all claims,
losses, and damages arising from or related to this Agreement,
with no cap on liability. Customer shall indemnify Provider
against any third-party claim arising from Customer's use of
the Services.

4. LIMITATION OF LIABILITY

Provider's total liability under this Agreement shall be
capped at fees paid in the 12 months preceding the claim.
Consequential damages are not excluded.

5. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. There is no termination for
convenience. There is no cure period.

6. NOTICES

All notices under this Agreement shall be in writing and
sent to the addresses set forth above.

7. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware, without regard to its conflict of laws provisions.
""")
    tools = store.as_document_tools()

    # --- Stage 1: redline_suggest ---
    print("=" * 70)
    print("STAGE 1: redline_suggest")
    print("=" * 70)
    suggest_agent = Agent(tools=tools, task="redline_suggest")
    suggest_result = suggest_agent.run(
        document_id="example-msa",
        schema=PLAYBOOK,
    )
    try:
        redline_package = json.loads(suggest_result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse suggest output as JSON: {e}")
        print("Raw output:")
        print(suggest_result.review)
        return 1

    print(f"Suggested {len(redline_package.get('proposed_redlines', []))} redlines")
    for r in redline_package.get("proposed_redlines", []):
        print(f"  - {r['clause_type']:25s} severity={r['severity']:8s} "
              f"section={r['section_ref']}")
    print(f"Suggest: {suggest_result.tool_calls} tool calls, "
          f"{suggest_result.elapsed_seconds:.1f}s")

    # --- Stage 2: redline_apply ---
    print()
    print("=" * 70)
    print("STAGE 2: redline_apply")
    print("=" * 70)

    # The schema parameter is the redline package from stage 1.
    apply_agent = Agent(tools=tools, task="redline_apply")
    apply_result = apply_agent.run(
        document_id="example-msa",
        schema=json.dumps(redline_package),  # the package becomes the schema
    )
    try:
        application = json.loads(apply_result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse apply output as JSON: {e}")
        print("Raw output:")
        print(apply_result.review)
        return 1

    # Print the application summary.
    summary = application.get("executive_summary", {})
    print(f"Total redlines: {summary.get('total_redlines', '?')}")
    print(f"Applied:        {summary.get('applied', '?')}")
    print(f"Pending review: {summary.get('pending_review', '?')}")
    print(f"Risk reduction: {summary.get('risk_reduction_estimate', '?')}")
    print(f"Apply: {apply_result.tool_calls} tool calls, "
          f"{apply_result.elapsed_seconds:.1f}s")

    # Print the change log.
    print()
    print("Change log:")
    for entry in application.get("change_log", []):
        print(f"  - {entry['clause_type']:25s} status={entry['status']:25s} "
              f"section={entry['section_ref']}")
        if entry.get("notes"):
            print(f"      notes: {entry['notes']}")

    # Print the unapplied redlines.
    if application.get("unapplied_redlines"):
        print()
        print("Unapplied redlines:")
        for u in application["unapplied_redlines"]:
            print(f"  - {u['clause_type']:25s} section={u['section_ref']}")
            print(f"      reason: {u['reason']}")
            print(f"      recommendation: {u['recommendation']}")

    # Save the redlined document to a file.
    redlined_doc = application.get("redlined_document", "")
    if redlined_doc:
        output_path = "/tmp/redlined-msa.txt"
        with open(output_path, "w") as f:
            f.write(redlined_doc)
        print()
        print(f"Redlined document saved to {output_path}")
        print(f"({len(redlined_doc)} chars)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
