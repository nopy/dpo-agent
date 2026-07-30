"""Example: redline suggestion against a firm's playbook.

This example shows how to:
1. Define a JSON playbook (firm's preferred language for each
   clause type, fallback language, red flags, negotiable
   sub-points).
2. Construct an Agent with task="redline_suggest" and pass
   the playbook as the `schema` parameter.
3. Get the redline package back as result.review (parse with
   json.loads).

For production: replace the InMemoryDocStore with a real
document store (CLM, S3, database) and write your own
DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


# A small but realistic playbook. In production this would be
# loaded from the firm's playbook database (a CMS, a YAML file,
# a database table). The keys (clause types) are firm-specific
# labels; the values follow the schema in the reviewer prompt.
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
    "confidentiality": {
        "preferred_language": "Mutual confidentiality obligations for 5 years post-termination. Excludes information that is (a) public, (b) already known, (c) independently developed, (d) rightfully received from a third party.",
        "fallback_language": "Mutual confidentiality for 3 years post-termination, with the same exclusions.",
        "red_flags": ["perpetual confidentiality", "no exclusions"],
        "negotiable": ["term length (3 to 7 years)", "exclusion list", "compelled disclosure exception"],
    },
    "termination": {
        "preferred_language": "Either party may terminate for material breach with 30 days' cure period. Either party may terminate for convenience with 90 days' written notice.",
        "fallback_language": "Either party may terminate for material breach with 60 days' cure period. Termination for convenience with 60 days' written notice.",
        "red_flags": ["no termination for convenience", "no cure period", "termination only for cause"],
        "negotiable": ["cure period (30 to 60 days)", "notice period (60 to 90 days)"],
    },
    "payment_terms": {
        "preferred_language": "Net 30 days from invoice date. Late payments accrue interest at 1.5% per month or the maximum legal rate, whichever is lower.",
        "fallback_language": "Net 45 days from invoice date. Late payments accrue interest at 1.0% per month.",
        "red_flags": ["Net 90 or longer", "no late-payment interest"],
        "negotiable": ["payment window (30 to 60 days)", "interest rate (1.0% to 1.5%)"],
    },
    "data_protection": {
        "preferred_language": "Provider acts as Processor under GDPR Art. 28. All 10 mandatory DPA terms included. Breach notification within 24 hours. International transfers governed by SCCs (2021/914).",
        "fallback_language": "Provider acts as Processor. Material DPA terms included; details in attached DPA. Breach notification within 48 hours.",
        "red_flags": ["no DPA", "no breach notification timeline", "international transfers without safeguards"],
        "negotiable": ["breach notification timeline (24 to 48 hours)", "transfer mechanism (SCCs vs IDTA)"],
    },
})


def main() -> int:
    # Use a small example MSA that intentionally deviates from
    # the playbook in several places. The agent should propose
    # redlines for the deviations and report the matches.
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as of
2024-03-01.

1. INDEMNIFICATION

Provider shall indemnify Customer against any and all claims,
losses, and damages arising from or related to this Agreement,
with no cap on liability. Customer shall indemnify Provider
against any third-party claim arising from Customer's use of
the Services.

2. LIMITATION OF LIABILITY

Provider's total liability under this Agreement shall be
capped at fees paid in the 12 months preceding the claim.
Consequential damages are not excluded.

3. CONFIDENTIALITY

Each party shall maintain the confidentiality of the other
party's confidential information for 3 years following
termination of this Agreement, with no exclusions.

4. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. There is no termination for
convenience.

5. PAYMENT TERMS

Customer shall pay all invoices within 60 days of receipt.
Late payments accrue interest at 1.0% per month.

6. DATA PROTECTION

Provider may process Customer data as necessary to provide
the Services. No separate Data Processing Agreement is
required.
""")
    tools = store.as_document_tools()

    # Construct the agent for the redline_suggest task.
    agent = Agent(tools=tools, task="redline_suggest")

    # Run with the playbook as the schema.
    result = agent.run(
        document_id="example-msa",
        schema=PLAYBOOK,
    )

    # The output is a JSON string. Parse it.
    try:
        redlines = json.loads(result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse agent output as JSON: {e}")
        print("Raw output:")
        print(result.review)
        return 1

    # Print the result.
    print("=" * 70)
    print("REDLINE PACKAGE")
    print("=" * 70)
    print(json.dumps(redlines, indent=2, ensure_ascii=False))
    print("=" * 70)
    print(f"Tool calls: {result.tool_calls}")
    print(f"Chunks read: {result.chunks_read}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
