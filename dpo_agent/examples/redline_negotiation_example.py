"""Example: full redline negotiation flow.

This example shows the THREE redline tasks working in
sequence:
1. redline_suggest — firm proposes redlines against a
   playbook
2. redline_apply — firm applies the redlines to produce a
   redlined document
3. redline_negotiation — counterparty responds with their
   own counter-proposal; firm produces a position-by-position
   analysis

In production, each of these stages has a human-in-the-loop
step. The example runs them all automatically to demonstrate
the pipeline.

For production: replace the InMemoryDocStore with a real
document store and write your own DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


# A firm's negotiation playbook. This is the firm's strategy
# per clause type — preferred_outcome, fallback_outcome,
# walk_away, and the concession_pattern.
NEGOTIATION_PLAYBOOK = json.dumps({
    "indemnification": {
        "preferred_outcome": "Mutual indemnification capped at 1x annual fees, excluding IP and confidentiality.",
        "fallback_outcome": "Mutual indemnification capped at 2x annual fees, with carve-outs for IP, confidentiality, and gross negligence.",
        "walk_away": "Uncapped indemnification, OR indemnification without any cap.",
        "BATNA": "We have 2 other vendors at this stage. Acceptable to walk away from this deal.",
        "concession_pattern": "Start with preferred. If counterparty pushes back to 2x with carve-outs, accept (it's our fallback). If they push beyond 2x OR refuse any cap, escalate.",
        "rationale": "Indemnification is the firm's #1 risk area. A 2x cap with carve-outs is acceptable; uncapped is a deal-breaker.",
    },
    "limitation_of_liability": {
        "preferred_outcome": "Total liability capped at 2x annual fees, with carve-outs for IP, confidentiality, gross negligence, willful misconduct.",
        "fallback_outcome": "Total liability capped at 1x annual fees, with the same carve-outs.",
        "walk_away": "Uncapped liability OR no carve-outs.",
        "BATNA": "Same as indemnification.",
        "concession_pattern": "Start with 2x. If they push to 1x, accept. If they push to 0.5x OR remove carve-outs, escalate.",
        "rationale": "Liability cap is the second-highest risk. 1x is acceptable; 0.5x is too low for our exposure.",
    },
    "payment_terms": {
        "preferred_outcome": "Net 30 days, 1.5% per month late-payment interest.",
        "fallback_outcome": "Net 45 days, 1.0% per month late-payment interest.",
        "walk_away": "Net 90+ days OR no late-payment interest.",
        "BATNA": "Same as indemnification.",
        "concession_pattern": "Start with Net 30 / 1.5%. If they push to Net 45 / 1.0%, accept. If they push to Net 60+ OR remove interest, escalate.",
        "rationale": "Net 30 is industry standard. Net 45 is acceptable for established vendors. Net 60+ signals financial distress.",
    },
    "termination": {
        "preferred_outcome": "Either party may terminate for material breach with 30 days' cure period. Either party may terminate for convenience with 90 days' notice.",
        "fallback_outcome": "Either party may terminate for material breach with 60 days' cure period. Termination for convenience with 60 days' notice.",
        "walk_away": "No termination for convenience OR no cure period.",
        "BATNA": "Same as indemnification.",
        "concession_pattern": "Start with 30/90. If they push to 60/60, accept. If they remove termination for convenience OR cure period, escalate.",
        "rationale": "Termination rights are non-negotiable for risk management. We will not sign a contract that locks us in for >2 years without an exit.",
    },
})


def main() -> int:
    # Use a small example MSA + a counterparty's counter-proposal.
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as
of 2024-03-01.

1. PAYMENT TERMS

Customer shall pay all invoices within 60 days of receipt.
Late payments accrue interest at 1.5% per month.

2. INDEMNIFICATION

Provider shall indemnify Customer against any and all claims,
losses, and damages arising from or related to this Agreement,
with no cap on liability.

3. LIMITATION OF LIABILITY

Provider's total liability shall be capped at fees paid in
the 12 months preceding the claim. Consequential damages
are not excluded.

4. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. There is no termination for
convenience. There is no cure period.

5. NOTICES

All notices under this Agreement shall be in writing and
sent to the addresses set forth above.

6. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware.
""")
    tools = store.as_document_tools()

    # --- Stage 1: redline_suggest ---
    print("=" * 70)
    print("STAGE 1: redline_suggest")
    print("=" * 70)
    suggest_agent = Agent(tools=tools, task="redline_suggest")
    # For this example, use the playbook's preferred_outcomes
    # as the suggester's "preferred_language" (so it produces
    # a coherent redline package). In production, the suggester
    # and negotiator have SEPARATE playbooks: the suggester's
    # is about clause language; the negotiator's is about
    # strategy.
    SUGGEST_PLAYBOOK = json.dumps({
        "indemnification": {
            "preferred_language": NEGOTIATION_PLAYBOOK and json.loads(NEGOTIATION_PLAYBOOK)["indemnification"]["preferred_outcome"],
            "fallback_language": json.loads(NEGOTIATION_PLAYBOOK)["indemnification"]["fallback_outcome"],
            "red_flags": ["uncapped indemnification"],
            "negotiable": ["cap multiplier", "carve-out list"],
        },
        "limitation_of_liability": {
            "preferred_language": json.loads(NEGOTIATION_PLAYBOOK)["limitation_of_liability"]["preferred_outcome"],
            "fallback_language": json.loads(NEGOTIATION_PLAYBOOK)["limitation_of_liability"]["fallback_outcome"],
            "red_flags": ["uncapped liability"],
            "negotiable": ["cap multiplier", "exclusion list"],
        },
        "payment_terms": {
            "preferred_language": json.loads(NEGOTIATION_PLAYBOOK)["payment_terms"]["preferred_outcome"],
            "fallback_language": json.loads(NEGOTIATION_PLAYBOOK)["payment_terms"]["fallback_outcome"],
            "red_flags": ["Net 90", "no late-payment interest"],
            "negotiable": ["payment window", "interest rate"],
        },
        "termination": {
            "preferred_language": json.loads(NEGOTIATION_PLAYBOOK)["termination"]["preferred_outcome"],
            "fallback_language": json.loads(NEGOTIATION_PLAYBOOK)["termination"]["fallback_outcome"],
            "red_flags": ["no termination for convenience"],
            "negotiable": ["cure period", "notice period"],
        },
    })
    suggest_result = suggest_agent.run(
        document_id="example-msa",
        schema=SUGGEST_PLAYBOOK,
    )
    try:
        redline_package = json.loads(suggest_result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse suggest output: {e}")
        return 1

    print(f"Suggested {len(redline_package.get('proposed_redlines', []))} redlines")

    # --- Stage 2: redline_apply ---
    print()
    print("=" * 70)
    print("STAGE 2: redline_apply")
    print("=" * 70)
    apply_agent = Agent(tools=tools, task="redline_apply")
    apply_result = apply_agent.run(
        document_id="example-msa",
        schema=json.dumps(redline_package),
    )
    try:
        application = json.loads(apply_result.review)
    except json.JSONDecodeError:
        application = {"executive_summary": {}}
    print(f"Applied: {application.get('executive_summary', {}).get('applied', '?')} redlines")

    # --- Stage 3: redline_negotiation ---
    # In production, the counterparty's counter-proposal is a
    # separate document. For this example, we simulate a
    # counter-proposal by writing a 2nd document with the
    # counterparty's response.
    store.add("counterparty-counter", """
COUNTERPARTY'S COUNTER-PROPOSAL (redlined by Widget Inc's counsel)

1. PAYMENT TERMS

Customer shall pay all invoices within 45 days of receipt.
Late payments accrue interest at 1.0% per month.

2. INDEMNIFICATION

Provider shall indemnify Customer against third-party claims
arising from Provider's gross negligence or willful misconduct,
capped at 2x annual fees paid by Customer, with carve-outs
for IP, confidentiality, and gross negligence.

3. LIMITATION OF LIABILITY

Provider's total liability shall be capped at 1x annual fees
paid in the 12 months preceding the claim. Consequential
damages are excluded.

4. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice and a 30-day cure period.
Either party may terminate for convenience with 60 days'
written notice.
""")
    print()
    print("=" * 70)
    print("STAGE 3: redline_negotiation")
    print("=" * 70)
    negotiate_agent = Agent(tools=tools, task="redline_negotiation")
    negotiate_result = negotiate_agent.run(
        document_id="counterparty-counter",  # the counterparty's response
        schema=NEGOTIATION_PLAYBOOK,         # the negotiation playbook
        jurisdiction_notes=(
            "Deal value: $500K annual contract. First deal with "
            "this counterparty. We have 2 other vendors at this "
            "stage. We are the larger party in the relationship."
        ),
    )
    try:
        brief = json.loads(negotiate_result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse negotiate output: {e}")
        print("Raw output:")
        print(negotiate_result.review)
        return 1

    # Print the brief.
    print(json.dumps(brief, indent=2, ensure_ascii=False))

    # Summary.
    print()
    print("=" * 70)
    print("NEGOTIATION BRIEF SUMMARY")
    print("=" * 70)
    summary = brief.get("executive_summary", {})
    print(f"Total disputed clauses: {len(brief.get('disputed_clauses', []))}")
    print(f"Acceptance clauses: {len(brief.get('acceptance_clauses', []))}")
    print(f"Walk-away risk: {len(brief.get('walk_away_risk', []))}")
    print(f"Risk trajectory: {summary.get('overall_risk_trajectory', '?')}")
    print()
    print("Recommended actions:")
    for clause in brief.get("disputed_clauses", []):
        action = clause.get("recommended_action", "?")
        ct = clause.get("clause_type", "?")
        ref = clause.get("section_ref", "?")
        print(f"  - {ct:25s} {ref:20s} → {action}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
