"""Retrieve — Layer 5 of the 8-layer GraphRAG pipeline.

Five retrieval mechanisms:
  1. **Vector search on entity embeddings** — top-K similar entities
     by text similarity (TF-IDF in this SQLite-only build; swap for
     embeddings in production)
  2. **Entity lookup** — by name or ID (exact match)
  3. **Path search** — shortest path between two parties
  4. **Temporal filtering** — edges/nodes with date filters

The agent layer (Layer 6, in dpo_agent/tasks/kg_agent) composes these.
The verification layer (Layer 7, in dpo_agent/kg/verify.py) checks
that the answer is grounded in the retrieved subgraph.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .llm import LLMProvider
from .store import GraphStore


class GraphQuery(BaseModel):
    """The agent's translation of a natural-language question into a graph query IR."""
    target_node: str = Field(..., description="The node label to match (Contract, Party, Clause, Obligation).")
    filters: Dict[str, str] = Field(
        default_factory=dict,
        description="Filters as key=value: 'effective_date__gte', 'effective_date__lte', 'governing_law__contains', 'total_amount__lte', 'deadline__lte', 'party__name'."
    )
    cypher: str = Field(..., description="The Cypher query (illustrative; not actually executed against the SQLite store).")
    explanation: str = Field(..., description="One-sentence explanation of the query intent.")


class SubgraphSummary(BaseModel):
    """A natural-language summary of a retrieved subgraph."""
    summary: str = Field(..., description="A 2-4 sentence summary of the subgraph.")
    key_findings: List[str] = Field(default_factory=list, description="Bullet list of key facts.")
    uncertainty: str = Field(default="", description="Honest statement of what's NOT known or unclear.")


# ─── Vector (text-similarity) search ───────────────────────────────

def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _tfidf_documents(docs: Dict[str, str]) -> Tuple[Dict[str, Counter], Dict[str, float]]:
    """Compute TF-IDF vectors for a dict of id → text. Returns (tfs, idf)."""
    tfs: Dict[str, Counter] = {}
    df: Counter = Counter()
    for doc_id, text in docs.items():
        tokens = _tokenize(text)
        tfs[doc_id] = Counter(tokens)
        for term in set(tokens):
            df[term] += 1
    n = len(docs)
    idf: Dict[str, float] = {term: math.log((n + 1) / (count + 1)) + 1 for term, count in df.items()}
    return tfs, idf


def _tfidf_vector(tf: Counter, idf: Dict[str, float]) -> Dict[str, float]:
    return {term: count * idf.get(term, 1.0) for term, count in tf.items()}


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[k] * b[k] for k in common)
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


# ─── The main Retriever ────────────────────────────────────────────

class Retriever:
    """Combines the 5 retrieval mechanisms over the GraphStore."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self._tfs: Optional[Dict[str, Counter]] = None
        self._idf: Optional[Dict[str, float]] = None

    def _build_index(self) -> None:
        """Build a TF-IDF index over contract summaries + party names + clause summaries."""
        if self._tfs is not None:
            return
        docs: Dict[str, str] = {}
        for c in self.store.all_contracts():
            text = " ".join(filter(None, [
                c.get("title") or "",
                c.get("summary") or "",
                c.get("contract_type") or "",
            ]))
            docs[f"contract:{c['contract_id']}"] = text
        for p in self.store.all_parties():
            docs[f"party:{p['party_id']}"] = p.get("name", "")
        for cl in self.store.all_clauses():
            docs[f"clause:{cl['clause_id']}"] = f"{cl.get('clause_type', '')} {cl.get('summary', '')}"
        self._tfs, self._idf = _tfidf_documents(docs)

    def vector_search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Return the top-K (entity_id, similarity) for the query."""
        self._build_index()
        assert self._tfs is not None and self._idf is not None
        q_tf = Counter(_tokenize(query))
        q_vec = _tfidf_vector(q_tf, self._idf)
        scored: List[Tuple[str, float]] = []
        for doc_id, tf in self._tfs.items():
            d_vec = _tfidf_vector(tf, self._idf)
            scored.append((doc_id, _cosine(q_vec, d_vec)))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def entity_lookup(self, name: str) -> List[dict]:
        """Find parties by name (case-insensitive substring)."""
        name_l = name.lower()
        results: list[dict] = []
        for p in self.store.all_parties():
            if name_l in p["name"].lower():
                results.append(p)
        return results

    def contracts_by_party(self, party_name: str) -> List[dict]:
        """All contracts involving a given party name."""
        return self.store.contracts_by_party(party_name)

    def contracts_by_governing_law(self, country: str) -> List[dict]:
        return self.store.contracts_by_governing_law(country)

    def contracts_by_year(self, year: int) -> List[dict]:
        return self.store.contracts_by_year(year)

    def obligations_due_before(self, date_str: str) -> List[dict]:
        return self.store.obligations_by_deadline(date_str)

    def shortest_path(self, from_party: str, to_party: str) -> List[str]:
        return self.store.shortest_path(from_party, to_party)

    # ─── Agent-facing: run a structured query ────────────────────────

    def run_query(self, query: GraphQuery) -> List[dict]:
        """Execute a GraphQuery and return the matching subgraph (as a list of dicts)."""
        target = query.target_node
        if target == "Contract":
            return self._query_contracts(query.filters)
        if target == "Party":
            return self.store.all_parties()
        if target == "Clause":
            return self.store.all_clauses()
        if target == "Obligation":
            return self.store.all_obligations()
        return []

    def _query_contracts(self, filters: Dict[str, str]) -> List[dict]:
        """Apply a set of filters to the contracts table."""
        results = self.store.all_contracts()
        if "effective_date__gte" in filters:
            results = [c for c in results if c.get("effective_date") and c["effective_date"] >= filters["effective_date__gte"]]
        if "effective_date__lte" in filters:
            results = [c for c in results if c.get("effective_date") and c["effective_date"] <= filters["effective_date__lte"]]
        if "governing_law__contains" in filters:
            needle = filters["governing_law__contains"].lower()
            results = [c for c in results if (c.get("governing_law_country") and needle in c["governing_law_country"].lower()) or (c.get("governing_law_state") and needle in c["governing_law_state"].lower())]
        if "total_amount__lte" in filters:
            try:
                cap = float(filters["total_amount__lte"])
                results = [c for c in results if c.get("total_amount") is not None and c["total_amount"] <= cap]
            except ValueError:
                pass
        if "total_amount__eq" in filters:
            try:
                amt = float(filters["total_amount__eq"])
                results = [c for c in results if c.get("total_amount") == amt]
            except ValueError:
                pass
        if "contract_type__eq" in filters:
            ct = filters["contract_type__eq"]
            results = [c for c in results if c.get("contract_type", "").upper() == ct.upper()]
        if "party__name" in filters:
            party = filters["party__name"]
            results = [c for c in results if any(party in p["name"] for p in self.store.parties_by_contract(c["contract_id"]))]
        return results

    def answer_question(self, question: str, *, provider: LLMProvider) -> SubgraphSummary:
        """End-to-end: question → GraphQuery → subgraph → SubgraphSummary.

        This is composed: vector/structured search, then LLM summary.
        The full dpo-agent version is in the kg_agent task; this is
        the pure-Python fallback for users who don't want the full
        task loop.
        """
        import json
        schema_hint = (
            "Allowed GraphQuery target_node: 'Contract', 'Party', 'Clause', 'Obligation'. "
            "Allowed filters: 'effective_date__gte', 'effective_date__lte', 'governing_law__contains', "
            "'total_amount__lte', 'total_amount__eq', 'party__name', 'deadline__lte', 'contract_type__eq'. "
            "Use the question wording to choose which filters to apply. "
            "If the question names a specific contract or party, use 'party__name' or filter on a known id."
        )
        try:
            gq: GraphQuery = provider.complete_structured(
                system="You translate natural-language questions about a contract knowledge graph into a GraphQuery IR.",
                user=f"Question: {question}\n\nAvailable nodes: Contract, Party, Clause, Obligation.\nAvailable filters: effective_date__gte/lte, governing_law__contains, total_amount__lte/eq, party__name, deadline__lte, contract_type__eq.\n\nProduce a GraphQuery with target_node + filters that, when applied, would answer the question.",
                response_model=GraphQuery,
                schema_hint=schema_hint,
            )
        except Exception as e:
            gq = GraphQuery(target_node="Contract", filters={}, cypher="", explanation=f"LLM failed: {e}; falling back to vector search.")
        nodes = self.run_query(gq)
        vector_hits = self.vector_search(question, top_k=5)
        nodes_json = json.dumps(nodes[:20], default=str)
        try:
            summary: SubgraphSummary = provider.complete_structured(
                system="You summarize retrieved graph subgraphs in plain English. Be concise. Note what is and isn't known.",
                user=(
                    f"Question: {question}\n\n"
                    f"GraphQuery: {gq.model_dump_json()}\n\n"
                    f"Subgraph ({len(nodes)} structured matches): {nodes_json}\n\n"
                    f"Vector hits: {vector_hits}\n\n"
                    f"Produce a SubgraphSummary answering the question."
                ),
                response_model=SubgraphSummary,
            )
        except Exception as e:
            summary = SubgraphSummary(
                summary=f"Found {len(nodes)} structured matches and {len(vector_hits)} vector hits. (LLM summarization failed: {e})",
                key_findings=[f"Structured match count: {len(nodes)}"],
                uncertainty=str(e),
            )
        return summary
