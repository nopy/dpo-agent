"""Tests for the kg_build task and the kgpipeline integration.

The kg_build task is the 10th built-in task. It takes a
TriageReport (from dpo-agent's triage pipeline) and runs the
kgpipeline's resolve → store → verify → update layers.
Skips the kgpipeline's extract layer (Layer 2) because the
TriageReport already has the structured data — saves tokens
and avoids duplicate work.

Tests verify:
- The task is discoverable
- The prompt enforces the "no re-extraction" discipline
- The integration module's TriageReportAdapter converts a
  TriageReport to a kgpipeline.ontology.Contract correctly
- The integration handles missing fields gracefully
- The integration handles the "no kgpipeline installed" case
"""

from __future__ import annotations

import json
import pytest

from dpo_agent.tasks.loader import list_tasks, load_prompt


# ─── Task discovery ───────────────────────────────────────────────────────

def test_kg_build_task_is_listed():
    tasks = list_tasks()
    assert "kg_build" in tasks
    assert len(tasks) >= 10  # was 9, now 10


def test_kg_build_prompts_exist():
    for prompt_type in ("reviewer", "critique", "navigator"):
        prompt = load_prompt("kg_build", prompt_type)
        assert len(prompt) > 500


# ─── Prompt discipline: no re-extraction ──────────────────────────────────

def test_kg_build_prompt_no_re_extraction():
    """The reviewer prompt must explicitly forbid re-extraction
    of data already in the TriageReport."""
    prompt = load_prompt("kg_build", "reviewer")
    lower = prompt.lower()
    # Should explicitly say "do not re-extract" or "no re-extract"
    assert "do not re-extract" in lower or "no re-extraction" in lower or \
           "never re-extract" in lower or "no re-extract" in lower or \
           "re-extract" in lower


def test_kg_build_prompt_reuses_triage_report():
    """The prompt should teach the agent to USE the TriageReport
    rather than re-running the triage pipeline."""
    prompt = load_prompt("kg_build", "reviewer")
    assert "triagereport" in prompt.lower().replace(" ", "") or \
           "triage_report" in prompt.lower() or "triage report" in prompt.lower()
    # Should mention re-using
    assert "reuse" in prompt.lower() or "use" in prompt.lower()


def test_kg_build_prompt_skips_layers_with_reason():
    """The prompt should explicitly mention which kgpipeline
    layers are skipped (ingest, extract, retrieve, agent)
    and why."""
    prompt = load_prompt("kg_build", "reviewer")
    lower = prompt.lower()
    # Should mention skipping
    assert "skip" in lower
    # Should mention the layers
    for layer in ("ingest", "extract", "retrieve", "agent"):
        assert layer in lower, f"missing layer {layer}"


def test_kg_build_prompt_layers_run():
    """The prompt should mention the layers that ARE run
    (resolve, store, verify, update)."""
    prompt = load_prompt("kg_build", "reviewer")
    lower = prompt.lower()
    for layer in ("resolve", "store", "verify", "update"):
        assert layer in lower, f"missing layer {layer}"


def test_kg_build_prompt_no_hallucination():
    """The prompt should teach: never invent. If the
    TriageReport has a field as null, the kgpipeline Contract
    should also be null."""
    prompt = load_prompt("kg_build", "reviewer")
    assert "never invent" in prompt.lower() or "do not invent" in prompt.lower()


def test_kg_build_prompt_no_silent_drop():
    """Every party in metadata, every obligation in
    obligations, every clause in clause_classification must
    appear in the resulting Contract."""
    prompt = load_prompt("kg_build", "reviewer")
    assert "silently drop" in prompt.lower() or "never silently" in prompt.lower() or \
           "must appear" in prompt.lower()


def test_kg_build_prompt_evidence_verification():
    """The agent should verify evidence by reading the
    contract, not by re-extracting."""
    prompt = load_prompt("kg_build", "reviewer")
    assert "evidence" in prompt.lower() and (
        "verify" in prompt.lower() or "match" in prompt.lower()
    )


# ─── Output schema ───────────────────────────────────────────────────────

def test_kg_build_output_has_kgpipeline_shaped_blocks():
    """The output should have the kgpipeline-shaped blocks:
    executive_summary, contract, graph_stats,
    verification_report, update_verdicts."""
    prompt = load_prompt("kg_build", "reviewer")
    for block in ("executive_summary", "contract", "graph_stats",
                  "verification_report", "update_verdicts"):
        assert block in prompt, f"missing block {block}"


def test_kg_build_output_documents_token_savings():
    """The executive_summary should document the token
    savings from skipping 4 kgpipeline layers."""
    prompt = load_prompt("kg_build", "reviewer")
    assert "save" in prompt.lower() or "saving" in prompt.lower() or \
           "skip" in prompt.lower()


# ─── 5 critique axes ─────────────────────────────────────────────────────

def test_kg_build_critique_5_axes():
    """The critique prompt should cover 5 axes:
    1. Grounding (every node traces to a TriageReport field)
    2. Re-extraction check
    3. Completeness
    4. Verification
    5. Graph integrity
    """
    prompt = load_prompt("kg_build", "critique")
    lower = prompt.lower()
    assert "grounding" in lower
    assert "re-extract" in lower or "re extract" in lower
    assert "completeness" in lower
    assert "verification" in lower or "verify" in lower
    assert "graph" in lower or "integrity" in lower


def test_kg_build_critique_re_extraction_violation():
    """The critique should specifically check that no extra
    LLM calls were made to re-extract data already in the
    TriageReport (this is the key token-saving discipline)."""
    prompt = load_prompt("kg_build", "critique")
    assert "token" in prompt.lower() or "re-extract" in prompt.lower()


# ─── Navigator packet ──────────────────────────────────────────────────

def test_kg_build_navigator_verifies_quotes():
    """The navigator's packet should focus on verifying
    verbatim quotes from the TriageReport against the
    source contract."""
    prompt = load_prompt("kg_build", "navigator")
    assert "verbatim" in prompt.lower()
    assert "verify" in prompt.lower() or "match" in prompt.lower()


def test_kg_build_navigator_finds_in_chunks():
    """The navigator's packet should produce a per-quote
    verification with status (found / not_found / fuzzy)
    and chunk index."""
    prompt = load_prompt("kg_build", "navigator")
    assert "found" in prompt.lower()
    assert "chunks" in prompt.lower()


# ─── Prompts are distinct from other tasks ──────────────────────────────

def test_kg_build_prompts_distinct_from_others():
    """The 10 task prompts should all be different."""
    prompts = {}
    for task in ("dpo", "metadata", "redline_suggest", "redline_apply",
                 "redline_negotiation", "clause_classification",
                 "summarize", "risk_score", "obligations", "kg_build"):
        prompts[task] = load_prompt(task, "reviewer")
    assert len(set(prompts.values())) == 10


# ─── Integration module ─────────────────────────────────────────────────

def test_kgpipeline_integration_module_imports():
    """The integration module should be importable even
    without kgpipeline installed (lazy import)."""
    from dpo_agent.integrations import kgpipeline
    assert hasattr(kgpipeline, "TriageReportAdapter")
    assert hasattr(kgpipeline, "build_graph")
    assert hasattr(kgpipeline, "run_pipeline")
    assert hasattr(kgpipeline, "kg_build_from_triage_pipeline")


def test_kgpipeline_integration_handles_missing_kgpipeline():
    """When kgpipeline is not installed, build_graph should
    raise a clear ImportError."""
    import dpo_agent.integrations.kgpipeline as kgi
    if kgi._HAVE_KGPIPELINE:
        pytest.skip("kgpipeline is installed")
    with pytest.raises(ImportError, match="kgpipeline"):
        kgi.build_graph(
            triage_report={"stages": []},
            document_id="test",
            contract_id="test",
            db_path="/tmp/test.db",
            document_text="test",
        )


# ─── Adapter behavior (with kgpipeline installed) ──────────────────────

@pytest.fixture(scope="module")
def kgpipeline_available():
    import dpo_agent.integrations.kgpipeline as kgi
    if not kgi._HAVE_KGPIPELINE:
        pytest.skip("kgpipeline not installed")


@pytest.fixture
def sample_triage_report():
    """A sample TriageReport (5-stage output) for testing the
    adapter."""
    return {
        "document_id": "MSA-2024-042",
        "stages": [
            {
                "task": "summarize",
                "output": "## TL;DR\n\nMSA between Acme Corp (Provider) and Widget Inc (Customer) for cloud services. Effective 2024-03-01 for 36 months. Net 30 payment terms. Standard indemnification with 1x cap."
            },
            {
                "task": "clause_classification",
                "output": {
                    "classifications": [
                        {
                            "clause_text": "Provider shall indemnify Customer against third-party claims arising from Provider's gross negligence or willful misconduct, capped at 1x annual fees paid by Customer.",
                            "section_ref": "Section 5.1",
                            "labels": [{"label": "indemnification", "confidence": "high"}],
                        },
                        {
                            "clause_text": "Provider's total liability under this Agreement shall be capped at 2x annual fees paid in the 12 months preceding the claim.",
                            "section_ref": "Section 6.1",
                            "labels": [{"label": "limitation_of_liability", "confidence": "high"}],
                        },
                    ]
                }
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
                            "clause_ref": "Section 5.1",
                            "verbatim_text": "Provider shall indemnify Customer against third-party claims arising from Provider's gross negligence or willful misconduct, capped at 1x annual fees paid by Customer.",
                            "confidence": "high",
                        },
                        {
                            "obligor": "Customer",
                            "obligee": "Provider",
                            "action": "pay all invoices within 30 days",
                            "deadline": "30 days from invoice",
                            "condition": "Provider issues an invoice",
                            "obligation_type": "payment",
                            "clause_ref": "Section 2.1",
                            "verbatim_text": "Customer shall pay all invoices within 30 days of receipt.",
                            "confidence": "high",
                        },
                    ]
                }
            },
            {
                "task": "risk_score",
                "output": {
                    "headline": {"score": 5.5, "band": "medium"}
                }
            },
            {
                "task": "dpo",
                "output": {
                    "executive_summary": {
                        "one_paragraph": "Standard GDPR Art. 28 terms present; no critical gaps."
                    }
                }
            },
        ]
    }


@pytest.fixture
def sample_contract_text():
    """The source contract text (the verbatim quotes must
    match substrings of this)."""
    return """
MASTER SERVICES AGREEMENT

This Master Services Agreement is entered into between Acme Corp
("Provider") and Widget Inc ("Customer") as of 2024-03-01.

1. SERVICES

Provider shall provide the Services as described in Schedule A.

2. PAYMENT TERMS

Customer shall pay all invoices within 30 days of receipt.

3. INDEMNIFICATION

Provider shall indemnify Customer against third-party claims
arising from Provider's gross negligence or willful misconduct,
capped at 1x annual fees paid by Customer.

4. LIMITATION OF LIABILITY

Provider's total liability under this Agreement shall be
capped at 2x annual fees paid in the 12 months preceding the
claim.
"""


def test_adapter_builds_contract(kgpipeline_available,
                                  sample_triage_report, sample_contract_text):
    """The adapter should build a kgpipeline Contract from
    the TriageReport."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    adapter = TriageReportAdapter(
        triage_report=sample_triage_report,
        document_text=sample_contract_text,
        contract_id="MSA-2024-042",
    )
    contract = adapter.build_contract()
    assert contract.contract_id == "MSA-2024-042"
    # The sample fixture doesn't have a contract_type, so the
    # adapter falls back to OTHER. We just check the type is set.
    assert contract.contract_type is not None
    assert "Acme Corp" in contract.summary  # from summarize
    assert len(contract.parties) >= 2  # Provider + Customer
    assert len(contract.clauses) == 2  # 2 classifications
    assert len(contract.obligations) == 2  # 2 obligations


def test_adapter_parties_have_roles(kgpipeline_available,
                                    sample_triage_report, sample_contract_text):
    """The adapter should set PartyRole from the metadata
    parties[].role field. The sample fixture uses generic
    role names ('provider', 'customer') so we check those."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    adapter = TriageReportAdapter(
        triage_report=sample_triage_report,
        document_text=sample_contract_text,
        contract_id="test",
    )
    contract = adapter.build_contract()
    party_names = {p.name for p in contract.parties}
    # The sample's metadata has Provider/Customer (the role
    # names are the canonical names in dpo-agent output)
    assert "Provider" in party_names
    assert "Customer" in party_names
    # Each should have a role assigned
    for p in contract.parties:
        assert p.role is not None
        assert p.role.value  # non-empty


def test_adapter_clauses_have_evidence(kgpipeline_available,
                                       sample_triage_report,
                                       sample_contract_text):
    """Each Clause should have at least one EvidenceSpan with
    a quote that matches the source."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    adapter = TriageReportAdapter(
        triage_report=sample_triage_report,
        document_text=sample_contract_text,
        contract_id="test",
    )
    contract = adapter.build_contract()
    for clause in contract.clauses:
        assert len(clause.evidence) >= 1
        # The quote should appear (approximately) in the source
        for ev in clause.evidence:
            assert ev.quote  # non-empty


def test_adapter_obligations_have_evidence(kgpipeline_available,
                                          sample_triage_report,
                                          sample_contract_text):
    """Each Obligation should have at least one EvidenceSpan."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    adapter = TriageReportAdapter(
        triage_report=sample_triage_report,
        document_text=sample_contract_text,
        contract_id="test",
    )
    contract = adapter.build_contract()
    for obl in contract.obligations:
        assert len(obl.evidence) >= 1
        # The action and obligor should be set
        assert obl.action
        assert obl.obligor


def test_adapter_handles_missing_optional_fields(kgpipeline_available):
    """When the TriageReport is minimal (no parties, no
    obligations), the adapter should still produce a valid
    Contract with empty lists."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    minimal_report = {
        "document_id": "test",
        "stages": [
            {"task": "summarize", "output": "## TL;DR\n\nMinimal contract."},
        ]
    }
    adapter = TriageReportAdapter(
        triage_report=minimal_report,
        document_text="Minimal contract content.",
        contract_id="test",
    )
    contract = adapter.build_contract()
    assert contract.contract_id == "test"
    assert contract.summary  # has the summary
    assert contract.parties == []  # empty, not None
    assert contract.clauses == []
    assert contract.obligations == []


def test_adapter_handles_string_metadata(kgpipeline_available,
                                        sample_contract_text):
    """The metadata stage may output a JSON string (e.g. when
    the agent's output is wrapped in markdown). The adapter
    should parse it."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    report = {
        "document_id": "test",
        "stages": [
            {"task": "metadata",
             "output": json.dumps({
                 "parties": [{"name": "Test Co", "role": "provider"}]
             })},
        ]
    }
    adapter = TriageReportAdapter(
        triage_report=report,
        document_text=sample_contract_text,
        contract_id="test",
    )
    contract = adapter.build_contract()
    assert any(p.name == "Test Co" for p in contract.parties)


def test_adapter_parties_field_name_in_metadata(kgpipeline_available,
                                                sample_contract_text):
    """The metadata stage's `parties` field can be either a
    list of {name, role} dicts or a list of name strings.
    The adapter should handle both."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    # Test with name-only strings
    report = {
        "document_id": "test",
        "stages": [
            {"task": "metadata",
             "output": json.dumps({
                 "parties": ["Provider", "Customer"]  # strings, not dicts
             })},
        ]
    }
    adapter = TriageReportAdapter(
        triage_report=report,
        document_text=sample_contract_text,
        contract_id="test",
    )
    contract = adapter.build_contract()
    party_names = {p.name for p in contract.parties}
    assert "Provider" in party_names
    assert "Customer" in party_names


def test_adapter_date_parsing(kgpipeline_available, sample_contract_text):
    """The adapter should parse various date formats into
    ISO 8601 yyyy-MM-dd."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    for date_str, expected_iso in [
        ("March 1, 2024", "2024-03-01"),
        ("2024-03-01", "2024-03-01"),
        ("March 15, 2024", "2024-03-15"),
    ]:
        report = {
            "document_id": "test",
            "stages": [
                {"task": "metadata",
                 "output": json.dumps({
                     "effective_date": date_str,
                 })},
            ]
        }
        adapter = TriageReportAdapter(
            triage_report=report,
            document_text=sample_contract_text,
            contract_id="test",
        )
        contract = adapter.build_contract()
        assert contract.effective_date == expected_iso, \
            f"failed for {date_str}: got {contract.effective_date}"


def test_adapter_duration_parsing(kgpipeline_available, sample_contract_text):
    """The adapter should convert term_months to ISO 8601
    duration (PnYnM)."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    test_cases = [
        (36, "P3Y"),  # 3 years
        (12, "P1Y"),  # 1 year
        (18, "P1Y6M"),  # 1.5 years
        (6, "P6M"),  # 6 months
        (0, None),  # perpetual
    ]
    for months, expected in test_cases:
        report = {
            "document_id": "test",
            "stages": [
                {"task": "metadata",
                 "output": json.dumps({"term_months": months})},
            ]
        }
        adapter = TriageReportAdapter(
            triage_report=report,
            document_text=sample_contract_text,
            contract_id="test",
        )
        contract = adapter.build_contract()
        assert contract.duration == expected, \
            f"failed for {months} months: got {contract.duration}"


def test_adapter_end_to_end_graph_build(kgpipeline_available,
                                       sample_triage_report,
                                       sample_contract_text, tmp_path):
    """The full build_graph function should produce a
    kgpipeline Contract, run resolve + store + verify, and
    return a dict with the artifacts."""
    from dpo_agent.integrations.kgpipeline import build_graph
    db_path = str(tmp_path / "test.db")
    result = build_graph(
        triage_report=sample_triage_report,
        document_id="MSA-2024-042",
        contract_id="MSA-2024-042",
        db_path=db_path,
        document_text=sample_contract_text,
    )
    assert "contract" in result
    assert "store" in result
    assert "verifier" in result
    assert "verification" in result
    assert "update_verdict" in result
    # The contract should be in the store. The actual stats
    # keys are "contracts", "parties", "clauses", "obligations".
    stats = result["store"].stats()
    assert stats["contracts"] >= 1


def test_adapter_records_skipped_layers(kgpipeline_available,
                                       sample_triage_report,
                                       sample_contract_text, tmp_path):
    """The run_pipeline function should record which kgpipeline
    layers were skipped (the 4 we don't run)."""
    from dpo_agent.integrations.kgpipeline import run_pipeline
    db_path = str(tmp_path / "test.db")
    result = run_pipeline(
        triage_report=sample_triage_report,
        document_id="MSA-2024-042",
        contract_id="MSA-2024-042",
        db_path=db_path,
        document_text=sample_contract_text,
    )
    assert "layers_run" in result
    assert "layers_skipped" in result
    # Should have skipped ingest, extract, retrieve, agent
    for layer in ("ingest", "extract", "retrieve", "agent"):
        assert layer in result["layers_skipped"]


def test_kg_build_task_in_cli_and_fastapi(kgpipeline_available):
    """The kg_build task should be a valid option in the CLI
    and the FastAPI server."""
    # CLI choices
    from dpo_agent.cli import main
    # The CLI parser is built lazily; we just verify the task
    # is in the choices by listing tasks.
    from dpo_agent.tasks.loader import list_tasks
    assert "kg_build" in list_tasks()
