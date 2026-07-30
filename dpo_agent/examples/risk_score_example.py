"""Example: contract risk scoring against a 6-dimension framework.

This example shows how to:
1. Define a risk framework (6 dimensions with weights and
   rubrics).
2. Construct an Agent with task="risk_score" and pass the
   framework as the `schema` parameter.
3. Get the multi-dimensional risk score back as result.review
   (parse with json.loads).

For production: replace the InMemoryDocStore with a real
document store (CLM, S3, database) and write your own
DocumentTools.
"""

from __future__ import annotations

import json

from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


# A 6-dimension risk framework with weights and rubrics.
# In production, this would be loaded from the firm's risk
# framework database (a YAML file, a CMS, a database table).
FRAMEWORK = json.dumps({
    "dimensions": [
        {
            "name": "legal",
            "weight": 0.25,
            "rubric": {
                "1-2": "Standard, balanced legal terms. Indemnification mutual with cap, liability cap present, governing law reasonable.",
                "3-4": "Slight deviations from market. Standard legal protections present but with minor gaps or one-sided terms.",
                "5-6": "Material deviations. Some standard legal protections missing or unusually one-sided.",
                "7-8": "Significant deviations. Multiple standard protections missing or materially one-sided.",
                "9-10": "Severe legal exposure. Critical protections absent or contract terms highly one-sided."
            }
        },
        {
            "name": "financial",
            "weight": 0.20,
            "rubric": {
                "1-2": "Standard payment terms (Net 30 or better), no auto-renewal or short auto-renewal with reasonable notice.",
                "3-4": "Payment terms slightly off market (Net 45) or auto-renewal slightly long.",
                "5-6": "Payment terms off market (Net 60+) or auto-renewal long (12+ months) with short notice.",
                "7-8": "Payment terms very long (Net 90+) or auto-renewal traps (12-month auto-renewal with 90-day notice).",
                "9-10": "Payment terms exploitative or pricing terms unclear/missing."
            }
        },
        {
            "name": "ip",
            "weight": 0.15,
            "rubric": {
                "1-2": "IP ownership clearly defined; pre-existing IP retained; work-for-hire present if applicable.",
                "3-4": "IP ownership mostly clear; some ambiguity about derivative works or pre-existing IP.",
                "5-6": "IP ownership ambiguous or one-sided; pre-existing IP unclear.",
                "7-8": "IP ownership highly one-sided (e.g. all IP transfers to customer without pre-existing carve-out).",
                "9-10": "No IP clause, or contract assigns all IP without any retention."
            }
        },
        {
            "name": "data_protection",
            "weight": 0.20,
            "rubric": {
                "1-2": "Full GDPR Art. 28 DPA terms; breach notification < 24h; sub-processor authorization; international transfers covered.",
                "3-4": "Most DPA terms present; some gaps (e.g. audit rights weak).",
                "5-6": "DPA missing several required terms; breach notification timeline 24-48h; international transfers not explicitly covered.",
                "7-8": "DPA missing critical terms (no sub-processor authorization, no audit rights); or international transfers with no safeguards.",
                "9-10": "No DPA, or DPA terms conflict with GDPR; or vendor processes personal data outside any documented safeguards."
            }
        },
        {
            "name": "operational",
            "weight": 0.10,
            "rubric": {
                "1-2": "SLAs defined; support response times specified; termination for cause and convenience both present.",
                "3-4": "SLAs mostly defined; some gaps in support or termination.",
                "5-6": "SLAs loose or missing; support terms vague; termination rights limited.",
                "7-8": "No SLAs; or termination only for cause with no cure period; or vendor can terminate at will.",
                "9-10": "No operational protections; vendor has unilateral rights to change services or terminate."
            }
        },
        {
            "name": "reputational",
            "weight": 0.10,
            "rubric": {
                "1-2": "No exclusivity, no non-compete, no unusual public disclosure requirements.",
                "3-4": "Limited non-compete (e.g. 6 months, narrow scope) or limited public disclosure.",
                "5-6": "Moderate non-compete (12 months) or public disclosure requirements.",
                "7-8": "Broad non-compete (24+ months) or unusual public disclosure (e.g. customer must approve vendor's marketing).",
                "9-10": "Perpetual non-compete or onerous public disclosure that could harm either party's brand."
            }
        }
    ]
})


def main() -> int:
    # Use a small but realistic MSA. The agent should produce
    # a multi-dimensional risk score with the headline aggregate
    # and per-dimension breakdowns.
    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as of
2024-03-01.

1. SERVICES

Provider shall provide the Services as described in Schedule A.

2. PAYMENT TERMS

Customer shall pay all invoices within 60 days of receipt.
Late payments accrue interest at 1.5% per month or the maximum
legal rate, whichever is lower.

3. CONFIDENTIALITY

Each party shall maintain the confidentiality of the other
party's confidential information for 5 years following
termination of this Agreement, with no exclusions.

4. IP OWNERSHIP

All intellectual property created by Provider in the course of
performing the Services shall be owned by Customer. Provider
retains rights to its pre-existing intellectual property.

5. INDEMNIFICATION

Provider shall indemnify Customer against any and all claims,
losses, and damages arising from or related to this Agreement,
with no cap on liability.

6. LIMITATION OF LIABILITY

Provider's total liability under this Agreement shall be
capped at fees paid in the 12 months preceding the claim.
Consequential damages are not excluded.

7. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. There is no termination for
convenience. There is no cure period.

8. AUTO-RENEWAL

The initial term is 36 months. The Agreement automatically
renews for successive 12-month periods unless either party
gives 90 days' written notice of non-renewal.

9. DATA PROTECTION

Provider may process Customer data as necessary to provide
the Services. No separate Data Processing Agreement is
required. No breach notification timeline. No sub-processor
authorization. International transfers are silent.

10. NOTICES

All notices under this Agreement shall be in writing and
sent to the addresses set forth above.

11. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware, without regard to its conflict of laws provisions.
""")
    tools = store.as_document_tools()

    # Construct the agent for the risk_score task.
    agent = Agent(tools=tools, task="risk_score")

    # Run with the framework as the schema.
    result = agent.run(
        document_id="example-msa",
        schema=FRAMEWORK,
    )

    # The output is a JSON string. Parse it.
    try:
        score = json.loads(result.review)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse agent output as JSON: {e}")
        print("Raw output:")
        print(result.review)
        return 1

    # Print the result.
    print("=" * 70)
    print("RISK SCORE")
    print("=" * 70)
    print(json.dumps(score, indent=2, ensure_ascii=False))
    print("=" * 70)
    print(f"Tool calls: {result.tool_calls}")
    print(f"Chunks read: {result.chunks_read}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")

    # Pretty-print a summary.
    if "headline" in score:
        h = score["headline"]
        print()
        print(f"Headline score: {h.get('score', '?')} ({h.get('band', '?')})")
        print(f"Confidence: {h.get('confidence', '?')}")
        if "confidence_interval" in h:
            lo, hi = h["confidence_interval"]
            print(f"Confidence interval: [{lo}, {hi}]")

    if "dimensions" in score:
        print()
        print("Per-dimension scores:")
        for d in score["dimensions"]:
            ci = d.get("confidence_interval", [None, None])
            print(f"  {d['name']:20s} {d.get('score', '?'):>5}  "
                  f"[{ci[0]}, {ci[1]}]  ({d.get('confidence', '?')})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
