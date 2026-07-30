"""Example: run the full triage pipeline on a single contract.

This example shows the typical end-to-end workflow:
1. Build a DocumentTools instance (here, the bundled
   in-memory store with an example contract).
2. Construct a TriagePipeline.
3. Run the default 5-task plan.
4. Print the markdown report.

The pipeline aggregates per-task results into a single
TriageReport with both markdown and JSON output. The
markdown report is what a human reads; the JSON is what a
downstream system (CLM, dashboard) consumes.

For production: replace the InMemoryDocStore with a real
document store. Add playbook, framework, and taxonomy JSON
strings as kwargs to enable the redline_suggest, risk_score,
and clause_classification stages.
"""

from __future__ import annotations

import json

from dpo_agent import (
    DocumentTools,
    PipelineConfig,
    TriagePipeline,
    DEFAULT_TRIAGE_PLAN,
)
from dpo_agent.examples.in_memory_tools import InMemoryDocStore


def main() -> int:
    # Use a small but realistic MSA.
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
Late payments accrue interest at 1.5% per month.

3. INDEMNIFICATION

Provider shall indemnify Customer against third-party claims
arising from Provider's gross negligence or willful
misconduct, capped at 1x annual fees paid by Customer,
excluding IP infringement and breach of confidentiality.
Customer shall indemnify Provider against any third-party
claim arising from Customer's use of the Services.

4. LIMITATION OF LIABILITY

Provider's total liability shall be capped at 2x annual fees
paid in the 12 months preceding the claim. Consequential
damages are excluded.

5. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. Either party may terminate for
convenience with 90 days' written notice.

6. DATA PROTECTION

Provider shall act as a Processor under GDPR Art. 28. All 10
mandatory DPA terms are included in the attached Data
Processing Addendum. Breach notification within 24 hours.

7. GOVERNING LAW

This Agreement shall be governed by the laws of the State of
Delaware.
""")
    tools = store.as_document_tools()

    # Construct the pipeline with the default 5-task plan.
    pipeline = TriagePipeline(
        tools=tools,
        config=PipelineConfig(),
    )
    # Print the plan so the user knows what's running.
    print(f"Pipeline plan ({len(DEFAULT_TRIAGE_PLAN)} tasks):")
    for i, task in enumerate(DEFAULT_TRIAGE_PLAN, 1):
        print(f"  {i}. {task}")
    print()

    # Run the pipeline.
    report = pipeline.run(
        document_id="example-msa",
        jurisdiction_notes=(
            "Provider is a US SaaS vendor; Customer has EU and "
            "US data subjects."
        ),
    )

    # Print summary.
    print("=" * 70)
    print("PIPELINE SUMMARY")
    print("=" * 70)
    print(f"Document: {report.document_id}")
    print(f"Total elapsed: {report.total_elapsed_seconds:.1f}s")
    print(f"Total cost estimate: ${report.total_cost_estimate:.2f}")
    print()
    for stage in report.stages:
        status = "✅" if stage.succeeded else "❌"
        print(f"  {status} {stage.task:25s} {stage.elapsed_seconds:6.1f}s "
              f"({stage.tool_calls} tool calls, "
              f"~${stage.cost_estimate:.2f})")

    # Print the markdown report.
    print()
    print("=" * 70)
    print("TRIAGE REPORT (markdown)")
    print("=" * 70)
    print(report.markdown)

    # Optionally save the JSON report.
    output_path = "/tmp/triage-report.json"
    with open(output_path, "w") as f:
        json.dump(report.json, f, indent=2)
    print()
    print(f"JSON report saved to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
