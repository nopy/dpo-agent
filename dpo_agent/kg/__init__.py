"""dpo_agent.kg — the local 8-layer GraphRAG pipeline for contracts.

A port of `wiki-contracts/kgpipeline/`. The Python code (Pydantic
schemas, GraphStore, parsers, MockLLM) lives here. The LLM-driven
layers (extract, resolve, agent, verify, update) are dpo-agent
tasks under `dpo_agent/tasks/kg_*/`.

Architecture (8 layers):
  1. ingest    — PDF/DOCX/HTML/TXT → text chunks (this module)
  2. extract   — LLM → validated Pydantic Contract (dpo_agent/tasks/kg_extract/)
  3. resolve   — entity dedup (this module + dpo_agent/tasks/kg_resolve/)
  4. store     — SQLite property graph (this module)
  5. retrieve  — vector + entity + path + temporal search (this module)
  6. agent     — long-context agent loop (dpo_agent/tasks/kg_agent/)
  7. verify    — evidence + ISO discipline (this module + dpo_agent/tasks/kg_verify/)
  8. update    — graph versioning (this module + dpo_agent/tasks/kg_update/)

The 4 dpo-agent tasks (kg_extract, kg_resolve, kg_agent, kg_verify,
kg_update) are the LLM-driven layer implementations. They use the
dpo-agent `Agent` class which is the same Anthropic client the
rest of dpo-agent uses — sharing context + prompt caching.

Quick start:

    from dpo_agent.kg import (
        Contract, ContractType, Party, PartyRole,
        GraphStore, Verifier, classify_update, resolve_parties,
        Retriever, MockLLM, get_provider,
    )

    # 1. Build a contract (or extract it via kg_extract task)
    contract = Contract(
        contract_id='MSA-2024-042',
        contract_type=ContractType.MSA,
        summary='...',
        parties=[...],
        ...
    )

    # 2. Persist to a graph DB
    store = GraphStore('contracts.db')
    store.upsert(contract)

    # 3. Verify
    verifier = Verifier(store)
    report = verifier.verify_contract(contract)

    # 4. Classify the update
    provider = get_provider('mock')  # or 'anthropic' / 'openai'
    verdict = classify_update(contract, store, provider=provider)
"""

from __future__ import annotations

# Re-export the public API.
from .ontology import (
    SCHEMA_VERSION,
    Clause,
    Contract,
    ContractType,
    DateField,
    EvidenceSpan,
    Location,
    MoneyAmount,
    Obligation,
    Party,
    PartyRole,
    CLAUSE_TYPES,
    get_clause_types,
    get_contract_types,
    get_party_roles,
)
from .ingest import (
    Chunk,
    Corpus,
    parse_directory,
    parse_file,
    parse_html,
    parse_pdf,
    parse_text,
)
from .store import GraphStore
from .llm import (
    LLMProvider,
    MockLLM,
    OpenAIProvider,
    AnthropicProvider,
    AgentLLMProvider,
    get_provider,
)
from .resolve import (
    ResolutionDecision,
    resolve_parties,
)
from .update import (
    UpdateClassification,
    UpdateVerdict,
    classify_update,
)
from .verify import (
    CheckResult,
    VerificationReport,
    Verifier,
)
from .retrieve import (
    GraphQuery,
    Retriever,
    SubgraphSummary,
)

__version__ = "0.2.0"

__all__ = [
    # Ontology
    "SCHEMA_VERSION",
    "Contract",
    "ContractType",
    "Party",
    "PartyRole",
    "Clause",
    "Obligation",
    "EvidenceSpan",
    "Location",
    "MoneyAmount",
    "DateField",
    "CLAUSE_TYPES",
    "get_clause_types",
    "get_contract_types",
    "get_party_roles",
    # Ingest
    "Chunk",
    "Corpus",
    "parse_file",
    "parse_directory",
    "parse_pdf",
    "parse_docx",
    "parse_html",
    "parse_text",
    # Store
    "GraphStore",
    # LLM
    "LLMProvider",
    "MockLLM",
    "OpenAIProvider",
    "AnthropicProvider",
    "AgentLLMProvider",
    "get_provider",
    # Resolve
    "ResolutionDecision",
    "resolve_parties",
    # Update
    "UpdateClassification",
    "UpdateVerdict",
    "classify_update",
    # Verify
    "CheckResult",
    "VerificationReport",
    "Verifier",
    # Retrieve
    "GraphQuery",
    "Retriever",
    "SubgraphSummary",
]
