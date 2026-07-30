"""Example: build a knowledge graph from a dpo-agent TriageReport.

This example shows the kgpipeline integration:
1. Run the dpo-agent triage pipeline on a contract
2. Convert the TriageReport to a kgpipeline Contract Pydantic
3. Run the kgpipeline's resolve + store + verify + update
   layers (skips ingest + extract because the TriageReport
   has the structured data)
4. Export the graph to Cypher (for Neo4j port)

Run with:
    ANTHROPIC_API_KEY=... python -m dpo_agent.examples.kgpipeline_integration_example

Without an API key, the dpo-agent pipeline will fail (it
needs an LLM to run the triage tasks). The integration
module itself runs without an API key — the adapter is
deterministic; only the kgpipeline's resolve + classify
steps need an LLM (and they fall back to deterministic dedup
if no provider is given).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Make sure dpo-agent can find kgpipeline
KG_PIPELINE_DIR = Path.home() / "wiki-contracts"
if str(KG_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(KG_PIPELINE_DIR))


def main() -> int:
    # Build the document store + tools
    from dpo_agent import (
        Agent,
        DocumentTools,
        PipelineConfig,
        TriagePipeline,
    )
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore

    store = InMemoryDocStore(chunk_size=4000)
    store.add("example-msa", """
MASTER SERVICES AGREEMENT

This Master Services Agreement ("Agreement") is entered into
between Acme Corp ("Provider") and Widget Inc ("Customer") as
of 2024-03-01.

1. SERVICES

Provider shall provide the Services as described in Schedule A.

2. PAYMENT TERMS

Customer shall pay all invoices within 30 days of receipt.
Late payments accrue interest at 1.5% per month.

3. INDEMNIFICATION

Provider shall indemnify Customer against third-party claims
arising from Provider's gross negligence or willful misconduct,
capped at 1x annual fees paid by Customer, excluding IP
infringement and breach of confidentiality.

4. LIMITATION OF LIABILITY

Provider's total liability under this Agreement shall be
capped at 2x annual fees paid in the 12 months preceding
the claim. Consequential damages are excluded.

5. TERMINATION

Either party may terminate this Agreement for material breach
with 30 days' written notice. Either party may terminate for
convenience with 90 days' written notice.

6. DATA PROTECTION

Provider shall act as a Processor under GDPR Art. 28. All 10
mandatory DPA terms are included in the attached Data
Processing Addendum. Breach notification within 24 hours.

7. NOTICES

All notices under this Agreement shall be in writing.

8. GOVERNING LAW

This Agreement shall be governed by the laws of the State
of Delaware.
""")
    tools = store.as_document_tools()
    document_text = list(store._docs.values())[0]  # for the adapter

    # ── 1. Run the dpo-agent triage pipeline ──
    print("=" * 70)
    print("STEP 1: Run the dpo-agent triage pipeline")
    print("=" * 70)
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        print("WARNING: no API key set. The triage pipeline will fail.")
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY to run end-to-end.")
        print()
        print("However, you can still see how the adapter works by")
        print("passing a pre-computed TriageReport. See the bottom of")
        print("this file for a hand-constructed example.")
        return _show_adapter_only(tools, store, document_text)

    pipeline = TriagePipeline(
        tools=tools,
        config=PipelineConfig(auto_confirm=True),
    )
    triage_report = pipeline.run(
        document_id="example-msa",
        jurisdiction_notes=(
            "Provider is a US SaaS vendor; Customer has EU "
            "and US data subjects."
        ),
    )
    print(f"Triage complete: {len(triage_report.stages)} stages, "
          f"{triage_report.total_elapsed_seconds:.1f}s, "
          f"~${triage_report.total_cost_estimate:.2f}")

    # ── 2. Build the knowledge graph ──
    print()
    print("=" * 70)
    print("STEP 2: Build the knowledge graph from the TriageReport")
    print("=" * 70)

    from dpo_agent.integrations.kgpipeline import (
        kg_build_from_triage_pipeline,
    )
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "example.db")
        cypher_path = str(Path(tmp) / "example.cypher")
        result = kg_build_from_triage_pipeline(
            pipeline_report=triage_report.json,
            document_text=document_text,
            db_path=db_path,
            contract_id="MSA-2024-042",
            export_cypher=cypher_path,
        )
        print(f"Contract:    {result['contract'].contract_id}")
        print(f"Type:        {result['contract'].contract_type.value}")
        print(f"Parties:     {len(result['contract'].parties)}")
        print(f"Clauses:     {len(result['contract'].clauses)}")
        print(f"Obligations: {len(result['contract'].obligations)}")
        print()
        print("Layers run (saved tokens by skipping the others):")
        for layer in result["layers_run"]:
            print(f"  + {layer}")
        print()
        print("Layers skipped (the TriageReport already had the data):")
        for layer in result["layers_skipped"]:
            print(f"  - {layer}")
        print()
        print(f"Graph stats: {result['store'].stats()}")
        print()
        print(f"Cypher export: {cypher_path}")
        cypher_content = Path(cypher_path).read_text()
        print(f"  ({len(cypher_content)} bytes, {cypher_content.count(chr(10))} lines)")
        print()
        print("First 10 lines of Cypher export:")
        for line in cypher_content.splitlines()[:10]:
            print(f"  {line}")

    return 0


def _show_adapter_only(tools, store, document_text):
    """Show the adapter with a hand-constructed TriageReport
    (no LLM needed)."""
    print()
    print("=" * 70)
    print("ALTERNATIVE: adapter with hand-constructed TriageReport")
    print("=" * 70)
    print("(This works without an API key; the dpo-agent stages")
    print("are simulated.)")
    print()

    from dpo_agent.integrations.kgpipeline import (
        kg_build_from_triage_pipeline,
    )

    # Hand-constructed TriageReport (what dpo-agent would produce)
    triage_report = {
        "document_id": "example-msa",
        "stages": [
            {
                "task": "summarize",
                "output": "## TL;DR\n\nMSA between Acme Corp (Provider) and Widget Inc (Customer) for cloud services. Effective 2024-03-01 for 36 months. Net 30 payment terms. Standard indemnification with 1x cap.",
            },
            {
                "task": "clause_classification",
                "output": {
                    "classifications": [
                        {
                            "clause_text": "Provider shall indemnify Customer against third-party claims arising from Provider's gross negligence or willful misconduct, capped at 1x annual fees paid by Customer.",
                            "section_ref": "Section 3",
                            "labels": [{"label": "indemnification", "confidence": "high"}],
                        },
                        {
                            "clause_text": "Provider's total liability under this Agreement shall be capped at 2x annual fees paid in the 12 months preceding the claim.",
                            "section_ref": "Section 4",
                            "labels": [{"label": "limitation_of_liability", "confidence": "high"}],
                        },
                    ]
                },
            },
            {
                "task": "obligations",
                "output": {
                    "obligations": [
                        {
                            "obligor": "Provider",
                            "obligee": "Customer",
                            "action": "indemnify against third-party claims arising from gross negligence",
                            "deadline": None,
                            "condition": "third-party claim",
                            "obligation_type": "indemnification",
                            "clause_ref": "Section 3",
                            "verbatim_text": "Provider shall indemnify Customer against third-party claims arising from Provider's gross negligence or willful misconduct, capped at 1x annual fees paid by Customer.",
                            "confidence": "high",
                        },
                    ]
                },
            },
            {
                "task": "metadata",
                "output": json.dumps({
                    "parties": [
                        {"name": "Acme Corp", "role": "provider"},
                        {"name": "Widget Inc", "role": "customer"},
                    ],
                    "effective_date": "2024-03-01",
                    "term_months": 36,
                    "governing_law": "Delaware, USA",
                }),
            },
            {
                "task": "risk_score",
                "output": {"headline": {"score": 5.5, "band": "medium"}},
            },
            {
                "task": "dpo",
                "output": {"executive_summary": {
                    "one_paragraph": "Standard GDPR Art. 28 terms present; no critical gaps."
                }},
            },
        ]
    }

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "example.db")
        cypher_path = str(Path(tmp) / "example.cypher")
        result = kg_build_from_triage_pipeline(
            pipeline_report=triage_report,
            document_text=document_text,
            db_path=db_path,
            contract_id="MSA-2024-042",
            export_cypher=cypher_path,
        )
        print(f"Contract:    {result['contract'].contract_id}")
        print(f"Type:        {result['contract'].contract_type.value}")
        print(f"Parties:     {len(result['contract'].parties)}")
        print(f"Clauses:     {len(result['contract'].clauses)}")
        print(f"Obligations: {len(result['contract'].obligations)}")
        print()
        print("Layers run (saved tokens by skipping the others):")
        for layer in result["layers_run"]:
            print(f"  + {layer}")
        print()
        print("Layers skipped (the TriageReport already had the data):")
        for layer in result["layers_skipped"]:
            print(f"  - {layer}")
        print()
        print(f"Graph stats: {result['store'].stats()}")
        print()
        print(f"Cypher export: {cypher_path}")
        cypher_content = Path(cypher_path).read_text()
        print(f"  ({len(cypher_content)} bytes, {cypher_content.count(chr(10))} lines)")
        print()
        print("First 10 lines of Cypher export:")
        for line in cypher_content.splitlines()[:10]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
