"""Ontology — the Pydantic schemas for the knowledge graph.

This is a port of `wiki-contracts/kgpipeline/ontology.py`,
the de facto reference contract ontology that mirrors the
Neo4j 2025 reference schema exactly. When persisted, the
Cypher export (in `dpo_agent/kg/store.py`) creates the
same nodes/edges in Neo4j.

Design rules followed (from [[schema-design]]):
- ISO standards for values: ISO 3166 (country), ISO 8601
  (date / duration), ISO 4217 (currency)
- Enums for closed sets (CONTRACT_TYPES, CLAUSE_TYPES,
  OBLIGATION_STATUS)
- `description=` on every field — the LLM uses these
- `Optional[str] = None` for fields that may be absent
  (no hallucination)
- Nest models, not flatten (Location inside Organization)
- Provenance: every contract/party/clause has a
  `source_chunk_id` and `evidence_span` linking back to
  the raw document
- Confidence: every edge has a `confidence_score` (0-1)

The dpo-agent integration: the `obligations` task produces
contracts that fit this schema; the `metadata` task produces
parties; the `clause_classification` task produces clauses.
The `kg_extract` task produces a full Contract that the
GraphStore can persist.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ─── Enumerations (closed sets) ─────────────────────────────────────

class ContractType(str, Enum):
    """The standard contract types. Expand per corpus."""
    NDA = "NDA"
    MSA = "MSA"
    SOW = "SOW"
    LEASE = "Lease"
    EMPLOYMENT = "Employment"
    SERVICE = "Service"
    LICENSE = "License"
    PARTNERSHIP = "Partnership"
    SALES = "Sales"
    CONSULTING = "Consulting"
    SETTLEMENT = "Settlement"
    OTHER = "Other"


class PartyRole(str, Enum):
    """How a party relates to the contract."""
    BUYER = "buyer"
    SELLER = "seller"
    EMPLOYER = "employer"
    EMPLOYEE = "employee"
    LESSOR = "lessor"
    LESSEE = "lessee"
    LICENSOR = "licensor"
    LICENSEE = "licensee"
    GUARANTOR = "guarantor"
    INDEMNITOR = "indemnitor"
    INDEMNITEE = "indemnitee"
    SUPPLIER = "supplier"
    CUSTOMER = "customer"
    OTHER = "other"


# 41 CUAD categories + a few common additions.
# Source: [[cuad]] paper (Hendrycks et al. 2021, arXiv:2103.06268)
CLAUSE_TYPES: List[str] = [
    "Affiliate License-Licensee", "Affiliate License-Licensor",
    "Anti-Assignment", "Audit Rights", "Cap On Liability",
    "Change Of Control", "Competitive Restriction Exception",
    "Covenant Not To Sue", "Document Name", "Effective Date",
    "Exclusivity", "Expiration Date", "Governing Law",
    "Insurance", "Ip Ownership Assignment", "Irrevocable Or Perpetual License",
    "Joint Ip Ownership", "License Grant", "Liquidated Damages",
    "Minimum Commitment", "Most Favored Nation", "No-Solicit Of Customers",
    "No-Solicit Of Employees", "Non-Compete", "Non-Disparagement",
    "Non-Transferable License", "Notice Period To Terminate Renewal",
    "Post-Termination Services", "Price Restrictions",
    "Renewal Term", "Revenue/Profit Sharing", "Rofr/Rofo/Rofn",
    "Source Code Escrow", "Third Party Beneficiary", "Termination For Cause",
    "Termination For Convenience", "Third Party Ip Ownership",
    "Uncapped Liability", "Unlimited/All-You-Can-Eat-License",
    "Volume Restriction", "Waiver Of Jury Trial", "Warranty Duration",
    # Additions beyond CUAD 41 — common in commercial contracts:
    "Confidentiality", "Force Majeure", "Indemnification",
    "Dispute Resolution", "Assignment", "Survival",
    "Notices", "Severability", "Entire Agreement",
    "Amendments", "Counterparts",
]


# ─── Building blocks ──────────────────────────────────────────────────────

class Location(BaseModel):
    """A physical or jurisdictional location. ISO 3166 for country."""
    address: Optional[str] = Field(
        None, description="The street address of the location. Use None if not provided."
    )
    city: Optional[str] = Field(
        None, description="The city of the location. Use None if not provided."
    )
    state: Optional[str] = Field(
        None, description="The state or region of the location. Use None if not provided."
    )
    country: Optional[str] = Field(
        None, description="The country of the location as ISO 3166 two-letter code (e.g. 'US', 'FR', 'JP'). Use None if not provided."
    )


class Party(BaseModel):
    """A party to a contract: organization or individual."""
    name: str = Field(
        ..., description="The full legal name of the party. For organizations, use the registered legal name (e.g. 'Acme Inc.' not 'Acme')."
    )
    role: PartyRole = Field(
        ..., description="The role of this party in the contract. Pick the most specific from the PartyRole enum."
    )
    location: Optional[Location] = Field(
        None, description="The party's primary location. Use None if not stated."
    )
    aliases: List[str] = Field(
        default_factory=list, description="Other names this party is referred to in the contract (e.g. 'the Company', 'Customer'). Filled in by the resolver."
    )
    # Provenance
    source_chunk_id: Optional[str] = Field(
        None, description="The chunk ID where this party was first identified."
    )
    confidence_score: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="LLM confidence in this extraction. Used by the verify layer."
    )


class DateField(BaseModel):
    """A date with provenance. ISO 8601 yyyy-MM-dd."""
    value: Optional[str] = Field(
        None, description="The date in ISO 8601 format (yyyy-MM-dd). If only the year is known, use yyyy-01-01. If not stated, return None."
    )
    raw_text: Optional[str] = Field(
        None, description="The original text as it appears in the contract (e.g. 'as of March 15, 2024')."
    )
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)


class MoneyAmount(BaseModel):
    """A monetary amount with currency. ISO 4217 currency code."""
    amount: Optional[float] = Field(
        None, description="The numerical amount. Return None if not stated."
    )
    currency: Optional[str] = Field(
        None, description="ISO 4217 currency code (e.g. 'USD', 'EUR', 'GBP'). Use None if not stated."
    )
    raw_text: Optional[str] = Field(
        None, description="The original text (e.g. '$50,000 USD', 'fifty thousand dollars')."
    )
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)


class EvidenceSpan(BaseModel):
    """Pointer to the source text that supports an extraction."""
    chunk_id: str = Field(..., description="The chunk ID where the evidence lives.")
    char_start: int = Field(..., ge=0, description="Character start offset in the chunk.")
    char_end: int = Field(..., ge=0, description="Character end offset in the chunk (exclusive).")
    quote: str = Field(..., description="The exact quote from the source text.")


class Clause(BaseModel):
    """A clause instance extracted from the contract."""
    clause_type: str = Field(
        ..., description=f"Allowed clause types are: {CLAUSE_TYPES}. Pick the most specific match. If none fit exactly, pick the closest."
    )
    summary: str = Field(
        ..., description="A short (1-2 sentence) summary of this clause in plain English. Do not use pronouns."
    )
    evidence: List[EvidenceSpan] = Field(
        default_factory=list, description="The source spans that support this clause. At least one is required for the verify layer."
    )
    confidence_score: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="LLM confidence in this clause extraction (0-1)."
    )


class Obligation(BaseModel):
    """A structured obligation per the obligation-extraction schema.

    The 5-field obligor/obligee/action/deadline/condition schema that
    powers CLM obligation tracking.
    """
    obligor: str = Field(
        ..., description="The party who must perform this obligation (canonical name)."
    )
    obligee: str = Field(
        ..., description="The party to whom the obligation is owed (canonical name)."
    )
    action: str = Field(
        ..., description="What the obligor must do (e.g. 'pay $10,000', 'deliver source code', 'maintain confidentiality for 3 years')."
    )
    deadline: Optional[str] = Field(
        None, description="The deadline in ISO 8601 (yyyy-MM-dd) or relative ('within 30 days of Effective Date'). Use None if no deadline."
    )
    condition: Optional[str] = Field(
        None, description="The condition that triggers the obligation (e.g. 'upon receipt of invoice', 'if Party A terminates without cause'). Use None if unconditional."
    )
    evidence: List[EvidenceSpan] = Field(
        default_factory=list, description="The source spans that support this obligation."
    )
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)


class Contract(BaseModel):
    """The root contract object. The de facto reference contract ontology.

    Mirrors the Neo4j 2025 Pydantic schema exactly. When persisted,
    the Cypher import creates Contract / Party / Location / Clause /
    Obligation nodes with the corresponding relationships.
    """
    contract_id: str = Field(
        ..., description="Unique identifier for this contract. Use the filename stem (e.g. 'MSA-2024-042') or a generated hash."
    )
    contract_type: ContractType = Field(
        ..., description="The type of contract. Pick from ContractType enum."
    )
    title: Optional[str] = Field(
        None, description="The contract's title as it appears on the first page."
    )
    summary: str = Field(
        ..., description="High-level summary of the contract (2-4 sentences). Include key parties, subject, and any unusual terms. Do not use pronouns."
    )
    parties: List[Party] = Field(
        default_factory=list, description="List of all parties to the contract with their roles and locations."
    )
    effective_date: Optional[str] = Field(
        None, description="The date the contract becomes effective in yyyy-MM-dd. If only year known, use yyyy-01-01. None if not stated."
    )
    end_date: Optional[str] = Field(
        None, description="The date the contract expires in yyyy-MM-dd. None if perpetual or not stated."
    )
    duration: Optional[str] = Field(
        None, description="ISO 8601 duration (e.g. 'P2Y' for 2 years, 'P18M' for 18 months)."
    )
    total_amount: Optional[MoneyAmount] = Field(
        None, description="Total monetary value of the contract."
    )
    governing_law: Optional[Location] = Field(
        None, description="The jurisdiction whose laws govern the contract."
    )
    clauses: List[Clause] = Field(
        default_factory=list, description="The clauses extracted from this contract, each with a CUAD-style type and a summary."
    )
    obligations: List[Obligation] = Field(
        default_factory=list, description="The obligations extracted from this contract, in the 5-field obligor/obligee/action/deadline/condition schema."
    )
    # Provenance & quality
    source_path: Optional[str] = Field(
        None, description="Filesystem path of the source document."
    )
    extraction_model: Optional[str] = Field(
        None, description="The LLM model used for extraction (e.g. 'gpt-4o-mini', 'claude-sonnet-4-5')."
    )
    schema_version: str = Field(
        default="0.2.0", description="The ontology schema version that produced this contract."
    )

    @field_validator("parties")
    @classmethod
    def _at_least_one_party(cls, v: List["Party"]) -> List["Party"]:
        # Soft check — we don't fail if a contract is missing parties,
        # but the verify layer will flag it.
        return v

    def party_names(self) -> List[str]:
        return [p.name for p in self.parties]

    def to_graph_dict(self) -> dict:
        """Serialize to a dict suitable for the SQLite store / Cypher export."""
        return self.model_dump(mode="json", exclude_none=False)


# The contract-ontology schema version. Bump when Contract / Party /
# Clause / Obligation fields change. The store layer uses this to
# detect schema drift and trigger re-ingestion.
SCHEMA_VERSION = "0.2.0"


def get_clause_types() -> List[str]:
    """Return the canonical clause-type list. The LLM is told this is closed."""
    return list(CLAUSE_TYPES)


def get_contract_types() -> List[str]:
    """Return the canonical contract-type enum."""
    return [t.value for t in ContractType]


def get_party_roles() -> List[str]:
    """Return the canonical party-role enum."""
    return [r.value for r in PartyRole]
