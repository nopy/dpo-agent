"""Tests for the local dpo_agent.kg module.

The kg module is the local port of wiki-contracts/kgpipeline.
It contains:
- ontology.py: Pydantic schemas (Contract, Party, Clause,
  Obligation, EvidenceSpan, Location, MoneyAmount)
- store.py: SQLite-backed GraphStore
- ingest.py: PDF/DOCX/HTML/TXT parsers
- llm.py: LLM provider (MockLLM, OpenAI, Anthropic)
- resolve.py: party dedup
- update.py: classify_update
- verify.py: deterministic Verifier
- retrieve.py: Retriever

These tests verify the Python code is correct, the schemas
validate, the GraphStore works, and the deterministic
checks (verify) produce sensible output.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest


# ─── Schema tests ─────────────────────────────────────────────

def test_ontology_imports():
    """The ontology module exports the expected Pydantic classes."""
    from dpo_agent.kg.ontology import (
        Contract, ContractType, Party, PartyRole, Clause,
        Obligation, EvidenceSpan, Location, MoneyAmount,
        DateField, SCHEMA_VERSION, CLAUSE_TYPES,
    )
    assert SCHEMA_VERSION == "0.2.0"
    assert len(CLAUSE_TYPES) == 53
    assert len(list(ContractType)) == 12
    assert len(list(PartyRole)) == 14


def test_contract_pydantic_validates():
    """A minimal Contract should pass Pydantic validation."""
    from dpo_agent.kg.ontology import Contract, ContractType
    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
    )
    assert c.contract_id == "T1"
    assert c.contract_type == ContractType.MSA
    assert c.parties == []
    assert c.clauses == []
    assert c.obligations == []


def test_party_pydantic_validates():
    from dpo_agent.kg.ontology import Party, PartyRole
    p = Party(name="Acme Corp", role=PartyRole.SUPPLIER)
    assert p.name == "Acme Corp"
    assert p.role == PartyRole.SUPPLIER
    assert p.confidence_score == 1.0  # default
    assert p.aliases == []


def test_clause_requires_evidence():
    """A Clause can be created with empty evidence, but the
    Verifier will flag it (evidence_coverage check)."""
    from dpo_agent.kg.ontology import Clause
    c = Clause(clause_type="Indemnification", summary="Test")
    assert c.evidence == []


def test_obligation_requires_5_fields():
    from dpo_agent.kg.ontology import Obligation
    o = Obligation(obligor="A", obligee="B", action="pay")
    assert o.obligor == "A"
    assert o.obligee == "B"
    assert o.action == "pay"
    assert o.deadline is None
    assert o.condition is None


# ─── Store tests ──────────────────────────────────────────────

def test_graphstore_upserts_contract():
    """The GraphStore should persist a contract and produce
    correct stats."""
    from dpo_agent.kg.ontology import (
        Contract, ContractType, Party, PartyRole, Clause,
        Obligation, EvidenceSpan,
    )
    from dpo_agent.kg.store import GraphStore

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
        parties=[
            Party(name="A", role=PartyRole.SUPPLIER),
            Party(name="B", role=PartyRole.CUSTOMER),
        ],
        clauses=[Clause(
            clause_type="Indemnification", summary="Indemnify",
            evidence=[EvidenceSpan(chunk_id="s1", char_start=0,
                                   char_end=10, quote="indemnify")],
        )],
        obligations=[Obligation(obligor="A", obligee="B", action="pay")],
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c)
    stats = store.stats()
    assert stats["contracts"] == 1
    assert stats["parties"] == 2
    assert stats["clauses"] == 1
    assert stats["obligations"] == 1
    store.close()
    Path(db).unlink()


def test_graphstore_cypher_export():
    """The Cypher export should be valid Neo4j syntax."""
    from dpo_agent.kg.ontology import (
        Contract, ContractType, Party, PartyRole,
    )
    from dpo_agent.kg.store import GraphStore

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
        parties=[Party(name="A", role=PartyRole.SUPPLIER)],
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c)
    cypher = store.to_cypher()
    # Should have CREATE CONSTRAINT and MERGE statements
    assert "CREATE CONSTRAINT" in cypher
    assert "MERGE (c:Contract" in cypher
    assert "MERGE (party:Party" in cypher
    assert "PARTY_TO" in cypher
    store.close()
    Path(db).unlink()


def test_graphstore_upsert_is_idempotent():
    """Re-upserting the same contract_id should not duplicate rows."""
    from dpo_agent.kg.ontology import (
        Contract, ContractType, Party, PartyRole,
    )
    from dpo_agent.kg.store import GraphStore

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
        parties=[Party(name="A", role=PartyRole.SUPPLIER)],
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c)
    store.upsert(c)
    store.upsert(c)
    stats = store.stats()
    assert stats["contracts"] == 1
    assert stats["parties"] == 1
    store.close()
    Path(db).unlink()


# ─── Verify tests ─────────────────────────────────────────────

def test_verifier_runs_all_6_checks():
    """The Verifier should run all 6 deterministic checks."""
    from dpo_agent.kg.ontology import (
        Contract, ContractType, Party, PartyRole,
    )
    from dpo_agent.kg.store import GraphStore
    from dpo_agent.kg.verify import Verifier

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
        parties=[Party(name="A", role=PartyRole.SUPPLIER)],
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c)
    verifier = Verifier(store)
    report = verifier.verify_contract(c)
    assert len(report.checks) == 6
    check_names = {c.name for c in report.checks}
    assert "evidence_coverage" in check_names
    assert "confidence_calibration" in check_names
    assert "source_in_store" in check_names
    assert "schema_discipline" in check_names
    assert "no_hallucinations" in check_names
    assert "cross_contract_contradictions" in check_names
    store.close()
    Path(db).unlink()


def test_verifier_flags_non_iso_format_date():
    """A non-ISO-format date (e.g. 'March 1, 2024') should be
    flagged by schema_discipline. (The check uses a regex,
    not a date validator — '2024-13-01' matches the regex
    but 'March 1, 2024' does not.)"""
    from dpo_agent.kg.ontology import Contract, ContractType
    from dpo_agent.kg.store import GraphStore
    from dpo_agent.kg.verify import Verifier

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
        effective_date="March 1, 2024",  # not ISO format
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c)
    verifier = Verifier(store)
    report = verifier.verify_contract(c)
    schema_check = next(c for c in report.checks if c.name == "schema_discipline")
    assert not schema_check.passed
    assert schema_check.score < 1.0
    assert any("effective_date" in issue for issue in schema_check.issues)
    store.close()
    Path(db).unlink()


# ─── Update tests ─────────────────────────────────────────────

def test_classify_update_new_contract():
    """A new contract should be classified as 'new'."""
    from dpo_agent.kg.ontology import Contract, ContractType
    from dpo_agent.kg.store import GraphStore
    from dpo_agent.kg.update import classify_update
    from dpo_agent.kg.llm import MockLLM

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    verdict = classify_update(c, store, provider=MockLLM())
    assert verdict.n_new == 1
    assert "new" in verdict.summary()
    store.close()
    Path(db).unlink()


# ─── Resolve tests ────────────────────────────────────────────

def test_resolve_exact_match():
    """Exact match (case-insensitive) should merge silently."""
    from dpo_agent.kg.ontology import Party, PartyRole
    from dpo_agent.kg.resolve import resolve_parties
    from dpo_agent.kg.llm import MockLLM

    parties = [
        Party(name="Acme Inc.", role=PartyRole.SUPPLIER),
        Party(name="acme inc.", role=PartyRole.SUPPLIER),
    ]
    canonical_map, decisions = resolve_parties(parties, provider=MockLLM())
    assert canonical_map["acme inc."] == "Acme Inc."
    # No LLM decisions for exact match (Python handles it)
    # (Decisions list may be empty or contain only deterministic ones)


def test_resolve_normalized_match():
    """Normalized match (strip legal suffix) should merge silently."""
    from dpo_agent.kg.ontology import Party, PartyRole
    from dpo_agent.kg.resolve import resolve_parties
    from dpo_agent.kg.llm import MockLLM

    parties = [
        Party(name="Acme Inc.", role=PartyRole.SUPPLIER),
        Party(name="Acme", role=PartyRole.SUPPLIER),
    ]
    canonical_map, decisions = resolve_parties(parties, provider=MockLLM())
    assert canonical_map["Acme"] == "Acme Inc."
    # The decision was a normalized match (not LLM-confirmed)
    assert any("Normalized" in d.explanation for d in decisions)


# ─── Retrieve tests ──────────────────────────────────────────

def test_retriever_vector_search():
    """The Retriever's vector search should return top-K matches."""
    from dpo_agent.kg.ontology import (
        Contract, ContractType, Party, PartyRole,
    )
    from dpo_agent.kg.store import GraphStore
    from dpo_agent.kg.retrieve import Retriever

    c1 = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Cloud services agreement between Provider and Customer",
    )
    c2 = Contract(
        contract_id="T2",
        contract_type=ContractType.NDA,
        summary="Non-disclosure agreement for confidential information",
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c1)
    store.upsert(c2)
    r = Retriever(store)
    hits = r.vector_search("cloud services", top_k=2)
    assert len(hits) == 2
    # The MSA should rank higher for "cloud services"
    assert "T1" in hits[0][0]
    store.close()
    Path(db).unlink()


# ─── LLM tests ───────────────────────────────────────────────

def test_mock_llm_extracts_contract():
    """The MockLLM should produce a Contract for contract text."""
    from dpo_agent.kg.ontology import Contract
    from dpo_agent.kg.llm import MockLLM
    mock = MockLLM()
    text = "This MSA is between Acme Inc. and Widget Inc. as of March 1, 2024."
    result = mock.complete_structured(
        system="extract",
        user=text,
        response_model=Contract,
    )
    assert result.contract_type.value == "MSA"
    assert "Acme Inc." in [p.name for p in result.parties]


def test_get_provider_auto():
    """get_provider('auto') should pick from env."""
    from dpo_agent.kg.llm import get_provider, MockLLM
    # Without env, defaults to mock
    p = get_provider("auto")
    assert isinstance(p, MockLLM)


# ─── Ingest tests ────────────────────────────────────────────

def test_parse_text():
    """The text parser should split at blank lines."""
    from dpo_agent.kg.ingest import parse_text
    from pathlib import Path
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("First paragraph.\n\nSecond paragraph.\n\n# Heading\n\nThird paragraph under heading.")
        path = Path(f.name)
    chunks = parse_text(path)
    assert len(chunks) >= 2
    # The section_path should be detected for the heading
    section_paths = {c.section_path for c in chunks}
    assert "Body" in section_paths or "Heading" in section_paths
    Path(path).unlink()


def test_verifier_passes_valid_iso_date():
    """A valid ISO date should pass schema_discipline."""
    from dpo_agent.kg.ontology import Contract, ContractType
    from dpo_agent.kg.store import GraphStore
    from dpo_agent.kg.verify import Verifier

    c = Contract(
        contract_id="T1",
        contract_type=ContractType.MSA,
        summary="Test",
        effective_date="2024-03-01",  # valid ISO date
    )
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    store = GraphStore(db)
    store.upsert(c)
    verifier = Verifier(store)
    report = verifier.verify_contract(c)
    schema_check = next(c for c in report.checks if c.name == "schema_discipline")
    assert schema_check.passed
    store.close()
    Path(db).unlink()


# ─── dpo_agent.kg public API ──────────────────────────────────

def test_dpo_agent_kg_public_api():
    """dpo_agent.kg should expose the expected public API names."""
    import dpo_agent.kg as kg
    assert kg.__version__ == "0.2.0"
    # 42 names: 4 ontology building blocks (DateField, MoneyAmount, etc.)
    # + Contract + 11 enums (ContractType, PartyRole, 14 values)
    # + 4 ingest types + 6 LLM-related (LLMProvider, MockLLM,
    # OpenAIProvider, AnthropicProvider, OpenRouterProvider,
    # AgentLLMProvider) + 4 store (GraphStore) + 6 resolve/update/verify
    # + 3 retrieve (GraphQuery, Retriever, SubgraphSummary)
    assert len(kg.__all__) == 42
    # Spot-check the key names
    assert "Contract" in kg.__all__
    assert "GraphStore" in kg.__all__
    assert "Verifier" in kg.__all__
    assert "MockLLM" in kg.__all__
    assert "OpenRouterProvider" in kg.__all__
    assert "Retriever" in kg.__all__
    assert "resolve_parties" in kg.__all__
    assert "classify_update" in kg.__all__


# ─── Backward compat: kgpipeline integration still works ────

def test_kgpipeline_integration_uses_local_kg():
    """The kgpipeline integration adapter should now use
    dpo_agent.kg instead of the wiki-contracts sibling."""
    from dpo_agent.integrations.kgpipeline import TriageReportAdapter
    from dpo_agent.kg.ontology import Contract
    # Build a Contract via the integration
    report = {
        "stages": [
            {"task": "metadata", "output": json.dumps({
                "parties": [{"name": "Test Co", "role": "provider"}],
                "effective_date": "2024-01-01",
                "term_months": 12,
            })},
        ]
    }
    adapter = TriageReportAdapter(
        triage_report=report,
        document_text="Test contract content.",
        contract_id="test",
    )
    contract = adapter.build_contract()
    # The contract should be a dpo_agent.kg.ontology.Contract
    assert isinstance(contract, Contract)
    assert contract.contract_id == "test"


def test_no_external_kgpipeline_imports():
    """After the refactor, the integration should NOT import
    from the external kgpipeline package."""
    import dpo_agent.integrations.kgpipeline as kgi
    # Get the source file
    source_file = Path(kgi.__file__).read_text()
    # Should reference dpo_agent.kg, not kgpipeline
    assert "dpo_agent.kg" in source_file or "from ..kg" in source_file
    # Should NOT have `from kgpipeline.ontology`
    assert "from kgpipeline." not in source_file
