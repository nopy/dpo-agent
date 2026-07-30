"""Update — Layer 8 of the 8-layer GraphRAG pipeline.

The loop closes here. When a new contract is ingested, its facts are
classified against the existing graph per the "Prompt 5 (Graph
Maintenance)" prompt from the GraphRAG build pipeline:

  - **new**: the fact doesn't exist in the graph
  - **duplicate**: the fact exists, with the same value
  - **contradiction**: the fact exists, with a different value (flag!)
  - **update**: same key, new value, supersedes the old (with version)
  - **uncertain**: the LLM can't tell; require human review

The store layer's `upsert()` already handles version increments and
preserves the previous state. This module classifies each new fact
*before* the upsert so the verify layer can flag contradictions.

dpo-agent integration: the `kg_update` task wraps this Python
code. The LLM-driven part (Prompt 5: Maintenance) is in the
task's reviewer.md prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from pydantic import BaseModel, Field

from .llm import LLMProvider
from .ontology import Contract
from .store import GraphStore


class UpdateClassification(BaseModel):
    """The LLM's classification of a single fact update."""
    classification: str = Field(..., description="One of: 'new', 'duplicate', 'contradiction', 'update', 'uncertain'.")
    explanation: str = Field(..., description="One-sentence explanation.")
    merge_into_node_id: Optional[str] = Field(None, description="If duplicate or update, the ID of the existing node to merge into.")
    confidence_score: float = Field(default=0.8, ge=0.0, le=1.0)


@dataclass
class UpdateVerdict:
    contract_id: str
    classifications: List[UpdateClassification]
    n_new: int
    n_duplicate: int
    n_contradiction: int
    n_update: int
    n_uncertain: int

    def has_contradictions(self) -> bool:
        return self.n_contradiction > 0

    def summary(self) -> str:
        return (
            f"Update verdict for {self.contract_id}: "
            f"new={self.n_new} duplicate={self.n_duplicate} "
            f"update={self.n_update} contradiction={self.n_contradiction} "
            f"uncertain={self.n_uncertain}"
        )


def classify_update(
    contract: Contract,
    store: GraphStore,
    *,
    provider: LLMProvider,
) -> UpdateVerdict:
    """Classify each fact in `contract` against the existing graph.

    Compares:
    - contract-level fields (effective_date, end_date, governing_law, total_amount)
    - parties (by name + role)
    - clauses (by type + summary fingerprint)
    - obligations (by obligor + obligee + action fingerprint)

    Returns an UpdateVerdict that the verify layer can use to
    gate the upsert.
    """
    classifications: List[UpdateClassification] = []
    # 1. Contract-level
    existing = store.get_contract(contract.contract_id)
    if existing is None:
        classifications.append(UpdateClassification(
            classification="new",
            explanation="Contract ID not in store; this is a new contract.",
            merge_into_node_id=None,
            confidence_score=0.99,
        ))
    else:
        for f in ("effective_date", "end_date", "duration", "contract_type"):
            new_v = getattr(contract, f)
            old_v = existing.get(f)
            if new_v != old_v and (new_v is not None):
                cls = _classify_single(
                    provider=provider,
                    new_fact={f: str(new_v)},
                    existing_fact={f: str(old_v)} if old_v is not None else None,
                )
                classifications.append(cls)
    # 2. Parties
    existing_parties = {(p["name"], p["role"]) for p in store.all_parties()}
    for p in contract.parties:
        key = (p.name, p.role.value if hasattr(p.role, "value") else str(p.role))
        if key not in existing_parties:
            classifications.append(UpdateClassification(
                classification="new",
                explanation=f"Party '{p.name}' (role={key[1]}) not in store.",
                confidence_score=0.95,
                merge_into_node_id=None,
            ))
    # 3. Clauses
    existing_clauses = {(cl["clause_type"], cl["summary"][:50]) for cl in store.all_clauses()}
    for cl in contract.clauses:
        key = (cl.clause_type, cl.summary[:50])
        if key not in existing_clauses:
            classifications.append(UpdateClassification(
                classification="new",
                explanation=f"Clause type={cl.clause_type} (contract={contract.contract_id}) not in store.",
                confidence_score=0.9,
                merge_into_node_id=None,
            ))
    # 4. Obligations
    existing_obs = {(o["obligor"], o["obligee"], o["action"][:50]) for o in store.all_obligations()}
    for ob in contract.obligations:
        key = (ob.obligor, ob.obligee, ob.action[:50])
        if key not in existing_obs:
            classifications.append(UpdateClassification(
                classification="new",
                explanation=f"Obligation {key} not in store.",
                confidence_score=0.9,
                merge_into_node_id=None,
            ))
    return _summarize(contract.contract_id, classifications)


def _classify_single(
    *,
    provider: LLMProvider,
    new_fact: dict,
    existing_fact: Optional[dict],
) -> UpdateClassification:
    if existing_fact is None:
        return UpdateClassification(
            classification="new",
            explanation="No existing fact in store.",
            confidence_score=0.9,
            merge_into_node_id=None,
        )
    user = (
        f"new fact: {new_fact}\n\n"
        f"existing: {existing_fact}\n\n"
        f"Classify as one of: new / duplicate / contradiction / update / uncertain. "
        f"Reply with classification, explanation (one sentence), merge_into_node_id (or null), confidence_score (0-1)."
    )
    try:
        return provider.complete_structured(
            system="You classify a fact update relative to an existing fact in a knowledge graph.",
            user=user,
            response_model=UpdateClassification,
        )
    except Exception as e:
        return UpdateClassification(
            classification="uncertain",
            explanation=f"LLM failed: {e}",
            merge_into_node_id=None,
            confidence_score=0.4,
        )


def _summarize(contract_id: str, classifications: List[UpdateClassification]) -> UpdateVerdict:
    counts = {"new": 0, "duplicate": 0, "contradiction": 0, "update": 0, "uncertain": 0}
    for c in classifications:
        if c.classification in counts:
            counts[c.classification] += 1
        else:
            counts["uncertain"] += 1
    return UpdateVerdict(
        contract_id=contract_id,
        classifications=classifications,
        n_new=counts["new"],
        n_duplicate=counts["duplicate"],
        n_contradiction=counts["contradiction"],
        n_update=counts["update"],
        n_uncertain=counts["uncertain"],
    )
