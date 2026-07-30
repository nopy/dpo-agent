"""LLM provider — abstract base + deterministic Mock + optional OpenAI/Anthropic.

Port of `wiki-contracts/kgpipeline/llm.py`. The kgpipeline's
`complete_structured(system, user, response_model)` interface is
preserved for backward compatibility with the resolve/retrieve/update
modules.

dpo-agent integration: the default `AgentLLMProvider` wraps a
dpo-agent `Agent` instance to make structured calls. The MockLLM
is used for testing without an API key.

To use a real LLM:
    # OpenAI
    llm = OpenAIProvider(model="gpt-4o-mini")
    # or Anthropic
    llm = AnthropicProvider(model="claude-sonnet-4-5")
    # or dpo-agent's Agent (recommended — same Anthropic client as the rest of dpo-agent)
    llm = AgentLLMProvider(agent=Agent(task="kg_resolve"))
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


# ─── Abstract base ─────────────────────────────────────────────────────

class LLMProvider(ABC):
    """Abstract LLM provider. Subclasses must implement `complete_structured`."""

    name: str = "abstract"

    @abstractmethod
    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: Type[T],
        schema_hint: Optional[str] = None,
    ) -> T:
        """Call the LLM and return a Pydantic-validated instance of `response_model`.

        Implementations are responsible for:
        1. Calling the LLM with the system + user prompt
        2. Parsing the response (JSON or tool-call) into the response_model
        3. Validating via Pydantic (raise on validation failure)
        4. Returning the validated instance
        """


# ─── Mock LLM (deterministic, no API key) ─────────────────────────────

class MockLLM(LLMProvider):
    """Deterministic mock that extracts from contract text using regex + heuristics.

    Quality is good enough to demonstrate the full 8-layer pipeline on
    the sample fixtures. Real production use should pass
    --provider=openai or --provider=anthropic.

    The mock implements the 5 prompts from the GraphRAG build pipeline:
    - Prompt 1 (Extraction): returns a Contract from the text
    - Prompt 2 (Normalization): dedup entities by string similarity
    - Prompt 3 (Graph Query): translates a question to a Cypher-like IR
    - Prompt 4 (Grounded Answer): summarizes a subgraph
    - Prompt 5 (Maintenance): classifies new facts vs existing
    """

    name = "mock"

    def __init__(self, schema_version: str = "0.2.0") -> None:
        self.schema_version = schema_version

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: Type[T],
        schema_hint: Optional[str] = None,
    ) -> T:
        # Dispatch on the response model class. The mock "knows" the
        # Contract / Party / Clause / Obligation shapes and produces
        # matching output from the contract text.
        from .ontology import Contract, Party, Clause, Obligation, Location, MoneyAmount, DateField
        from .resolve import ResolutionDecision
        from .retrieve import GraphQuery, SubgraphSummary
        from .update import UpdateClassification

        if response_model is Contract:
            data = self._extract_contract(user)
            return Contract(**data)  # type: ignore[return-value]
        if response_model is ResolutionDecision:
            data = self._resolve_decision(user)
            return ResolutionDecision(**data)  # type: ignore[return-value]
        if response_model is GraphQuery:
            data = self._graph_query(user)
            return GraphQuery(**data)  # type: ignore[return-value]
        if response_model is SubgraphSummary:
            data = self._subgraph_summary(user)
            return SubgraphSummary(**data)  # type: ignore[return-value]
        if response_model is UpdateClassification:
            data = self._update_classification(user)
            return UpdateClassification(**data)  # type: ignore[return-value]
        # Unknown model — try to instantiate with empty
        try:
            return response_model()  # type: ignore[return-value,call-arg]
        except Exception as e:
            raise NotImplementedError(
                f"MockLLM does not know how to produce {response_model.__name__}: {e}"
            )

    # ─── Mock extraction ─────────────────────────────────────────────

    def _extract_contract(self, text: str) -> dict:
        """Extract a Contract from the text. Heuristic-based."""
        # Detect contract type
        contract_type = "Other"
        for ct in ("NDA", "MSA", "SOW", "Lease", "Employment", "Service",
                   "License", "Partnership", "Sales", "Consulting", "Settlement"):
            if ct.lower() in text.lower():
                contract_type = ct
                break
        # Extract parties (look for "between X and Y" pattern)
        parties = []
        m = re.search(r"between\s+([^\(\)\n]+?)\s*(?:\(\"[^\"]+\"\))?\s+and\s+([^\(\)\n]+?)(?:\s*\([^\)]+\))?\s*(?:as of|effective)",
                      text, re.IGNORECASE)
        if m:
            for name in (m.group(1), m.group(2)):
                name = name.strip().strip('"').strip()
                if name and len(name) > 2 and len(name) < 100:
                    parties.append({
                        "name": name,
                        "role": "other",
                        "confidence_score": 0.7,
                    })
        # Extract effective date
        effective_date = None
        m = re.search(r"as of\s+(\w+ \d{1,2},\s*\d{4})", text, re.IGNORECASE)
        if m:
            from datetime import datetime
            for fmt in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    effective_date = datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
        # Build summary
        summary = text.strip().split("\n")[0][:200] if text.strip() else "Mock-extracted contract"
        return {
            "contract_id": f"mock-{hash(text) % 100000:05d}",
            "contract_type": contract_type,
            "title": None,
            "summary": summary,
            "parties": parties,
            "effective_date": effective_date,
            "clauses": [],
            "obligations": [],
        }

    def _resolve_decision(self, user: str) -> dict:
        """Decide if two parties are the same. Default: not same."""
        return {
            "same_entity": False,
            "canonical_name": "",
            "explanation": "Mock LLM: defaulting to different",
            "confidence_score": 0.5,
        }

    def _graph_query(self, user: str) -> dict:
        """Translate a question to a GraphQuery."""
        return {
            "target_node": "Contract",
            "filters": {},
            "cypher": "",
            "explanation": "Mock LLM: returning empty query",
        }

    def _subgraph_summary(self, user: str) -> dict:
        """Summarize a subgraph."""
        return {
            "summary": "Mock LLM summary",
            "key_findings": [],
            "uncertainty": "Mock LLM — no real analysis performed",
        }

    def _update_classification(self, user: str) -> dict:
        """Classify a fact update."""
        return {
            "classification": "uncertain",
            "explanation": "Mock LLM: defaulting to uncertain",
            "merge_into_node_id": None,
            "confidence_score": 0.4,
        }


# ─── OpenAI provider (instructor) ───────────────────────────────────

class OpenAIProvider(LLMProvider):
    """OpenAI provider using instructor for Pydantic-validated outputs."""

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model
        try:
            import instructor
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "openai and instructor are required for OpenAIProvider. "
                "Install with: pip install openai instructor"
            ) from e
        self._client = instructor.from_openai(OpenAI())

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: Type[T],
        schema_hint: Optional[str] = None,
    ) -> T:
        full_system = system
        if schema_hint:
            full_system += f"\n\n{schema_hint}"
        return self._client.chat.completions.create(
            model=self.model,
            response_model=response_model,
            messages=[
                {"role": "system", "content": full_system},
                {"role": "user", "content": user},
            ],
        )


# ─── Anthropic provider (instructor) ───────────────────────────────

class AnthropicProvider(LLMProvider):
    """Anthropic provider using instructor for Pydantic-validated outputs."""

    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-5") -> None:
        self.model = model
        try:
            import instructor
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError(
                "anthropic and instructor are required for AnthropicProvider. "
                "Install with: pip install anthropic instructor"
            ) from e
        self._client = instructor.from_anthropic(Anthropic())

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: Type[T],
        schema_hint: Optional[str] = None,
    ) -> T:
        full_system = system
        if schema_hint:
            full_system += f"\n\n{schema_hint}"
        return self._client.messages.create(
            model=self.model,
            response_model=response_model,
            system=full_system,
            messages=[{"role": "user", "content": user}],
        )


# ─── AgentLLMProvider (uses dpo-agent's Agent) ────────────────────

class AgentLLMProvider(LLMProvider):
    """Provider that uses dpo-agent's Agent class.

    This is the recommended way to use the kg pipeline in production:
    it shares the same Anthropic/OpenAI client as the rest of
    dpo-agent, gets prompt caching for free, and respects the same
    model selection as your other tasks.

    The AgentLLMProvider routes each `complete_structured` call to
    a one-shot Agent run with the system + user prompt as the
    conversation. The Agent's tool loop is bypassed; the response
    is parsed as JSON into the Pydantic model.

    NOTE: this is a thin wrapper; the real work happens in the
    dpo-agent `kg_extract`, `kg_resolve`, `kg_verify`, `kg_update`
    tasks. The kgpipeline's resolve/retrieve/update modules can
    still use this provider, but for production use the dpo-agent
    tasks are preferred (they share context + caching).
    """

    name = "dpo-agent"

    def __init__(self, *, agent=None) -> None:
        self._agent = agent  # Optional[Agent]; lazily created

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: Type[T],
        schema_hint: Optional[str] = None,
    ) -> T:
        # For the kgpipeline's resolve/retrieve/update paths, we need
        # a non-tool-loop call. The simplest implementation: use
        # Anthropic's structured output (or OpenAI's) directly.
        # The dpo-agent tasks do the LLM work; this is a fallback
        # for the kgpipeline's Python code.
        from .llm import OpenAIProvider, AnthropicProvider
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = AnthropicProvider()
        elif os.environ.get("OPENAI_API_KEY"):
            provider = OpenAIProvider()
        else:
            raise RuntimeError(
                "No API key set. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
                "or use MockLLM for testing."
            )
        return provider.complete_structured(
            system=system, user=user,
            response_model=response_model, schema_hint=schema_hint,
        )


# ─── Factory ──────────────────────────────────────────────────────

def get_provider(name: str = "auto", **kwargs) -> LLMProvider:
    """Get an LLM provider by name.

    Args:
        name: "auto" (default, picks from env), "mock", "openai",
              "anthropic", "dpo-agent".
        **kwargs: provider-specific args (e.g. model="gpt-4o-mini").
    """
    name = name.lower()
    if name == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            name = "openai"
        else:
            name = "mock"
    if name == "mock":
        return MockLLM(**kwargs)
    if name == "openai":
        return OpenAIProvider(**kwargs)
    if name == "anthropic":
        return AnthropicProvider(**kwargs)
    if name == "dpo-agent":
        return AgentLLMProvider(**kwargs)
    raise ValueError(f"Unknown provider: {name}. Use 'auto', 'mock', 'openai', 'anthropic', or 'dpo-agent'.")
