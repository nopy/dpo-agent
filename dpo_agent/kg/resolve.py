"""Resolve — Layer 3 of the 8-layer GraphRAG pipeline.

Deduplicates entities that the LLM has named differently.
"Acme Inc." / "ACME" / "Acme Incorporated" / "Acme Inc" all refer
to the same entity. The LLM does not always canonicalize.

Strategy:
  1. Exact match (case-insensitive) → merge
  2. Normalized match (strip suffixes, punctuation) → merge
  3. Fuzzy match (Jaccard / Levenshtein ratio) → ask LLM to confirm
  4. LLM-only match (for ambiguous cases) → ask LLM to decide

The output is a list of `ResolutionDecision` records plus a mapping
from original name → canonical name. The store layer applies this
mapping to dedupe nodes.

dpo-agent integration: the `kg_resolve` task wraps this Python
code. The LLM-driven part (Prompt 2: Normalization) is in the
task's reviewer.md prompt.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .llm import LLMProvider
from .ontology import Party


class ResolutionDecision(BaseModel):
    """The LLM's decision on whether two entities are the same."""
    same_entity: bool = Field(..., description="True if the two entities refer to the same real-world entity.")
    canonical_name: str = Field(..., description="The canonical name to use for the merged entity.")
    explanation: str = Field(..., description="Brief justification of the decision.")
    confidence_score: float = Field(default=0.9, ge=0.0, le=1.0)


# ─── Heuristic helpers ──────────────────────────────────────────────

LEGAL_SUFFIXES = [
    # Order matters: longest first, to avoid partial matches (e.g. "inc" before "incorporated")
    " incorporated", " corporation", " company",
    " public limited company",
    " plc",
    " inc.", " inc", " llc", " l.l.c.", " ltd.", " ltd",
    " corp.", " corp", " co.", " co",
    " s.a.s.", " sas", " s.a.", " s.a",
    " s.à r.l.", " sàrl",
    " gmbh", " ag",
    " bv", " nv",
    " ab", " aktiebolag", " asa",
    " as", " pty.", " pty",
    " sp. z.o.o.", " sp.z.o.o.",
    " s.r.l.", " srl",
    " k.k.", " kk",
    " pte ltd", " pte.",
]


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy comparison: lowercase, strip suffixes, alnum only."""
    n = name.lower().strip()
    for suffix in LEGAL_SUFFIXES:
        n = n.replace(suffix, "")
    n = re.sub(r"[^a-z0-9]", "", n)
    return n


def _exact_same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _normalized_same(a: str, b: str) -> bool:
    return _normalize_name(a) == _normalize_name(b) and _normalize_name(a) != ""


def _fuzzy_ratio(a: str, b: str) -> float:
    """A simple Jaccard similarity over character n-grams (n=2)."""
    a_n = _normalize_name(a)
    b_n = _normalize_name(b)
    if not a_n or not b_n:
        return 0.0
    n = 2
    a_grams = {a_n[i:i + n] for i in range(len(a_n) - n + 1)}
    b_grams = {b_n[i:i + n] for i in range(len(b_n) - n + 1)}
    if not a_grams or not b_grams:
        return 0.0
    inter = len(a_grams & b_grams)
    union = len(a_grams | b_grams)
    return inter / union if union else 0.0


# ─── Main API ──────────────────────────────────────────────────────

def resolve_parties(
    parties: List[Party],
    *,
    provider: LLMProvider,
    fuzzy_threshold: float = 0.7,
) -> Tuple[Dict[str, str], List[ResolutionDecision]]:
    """Deduplicate a list of Party objects.

    Returns:
        canonical_map: dict from original name → canonical name
        decisions: list of LLM decisions made (for audit / store)

    Strategy:
      1. Exact match (case-insensitive) → merge silently
      2. Normalized match (suffix-stripped) → merge silently
      3. Fuzzy match above threshold → ask LLM to confirm
      4. LLM-only decisions logged
    """
    canonical_map: Dict[str, str] = {}
    decisions: List[ResolutionDecision] = []
    canonical: List[Party] = []  # the deduped list
    for party in parties:
        merged = False
        for existing in canonical:
            if _exact_same(party.name, existing.name):
                canonical_map[party.name] = existing.name
                merged = True
                break
            if _normalized_same(party.name, existing.name):
                canonical_map[party.name] = existing.name
                decisions.append(ResolutionDecision(
                    same_entity=True,
                    canonical_name=existing.name,
                    explanation=f"Normalized match: '{party.name}' → '{existing.name}'",
                    confidence_score=0.95,
                ))
                merged = True
                break
            ratio = _fuzzy_ratio(party.name, existing.name)
            if ratio >= fuzzy_threshold:
                # Ask the LLM to confirm
                decision = _llm_decide(party, existing, provider)
                decisions.append(decision)
                if decision.same_entity:
                    canonical_map[party.name] = decision.canonical_name
                    # Merge aliases
                    if party.name != existing.name:
                        existing.aliases.append(party.name)
                    merged = True
                    break
        if not merged:
            canonical.append(party)
            canonical_map[party.name] = party.name
    # Apply canonical_map to each party's name
    for party in parties:
        if party.name in canonical_map:
            party.name = canonical_map[party.name]
    return canonical_map, decisions


def _llm_decide(a: Party, b: Party, provider: LLMProvider) -> ResolutionDecision:
    """Ask the LLM whether two parties are the same entity."""
    user = (
        f"Are these two parties the same legal entity?\n\n"
        f"Party A: {a.model_dump_json()}\n\n"
        f"Party B: {b.model_dump_json()}\n\n"
        f"Reply with: same_entity (bool), canonical_name (the name to use), "
        f"explanation (one sentence), confidence_score (0-1)."
    )
    try:
        return provider.complete_structured(
            system="You are a legal entity resolution expert. Compare two party records and decide if they refer to the same legal entity.",
            user=user,
            response_model=ResolutionDecision,
        )
    except Exception:
        # On failure, default to "different" (safer)
        return ResolutionDecision(
            same_entity=False,
            canonical_name=a.name,
            explanation="LLM call failed; defaulting to different",
            confidence_score=0.5,
        )
