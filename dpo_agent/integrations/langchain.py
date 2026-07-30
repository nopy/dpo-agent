"""LangChain / deepagents integration.

Wrap each dpo-agent task as a LangChain tool. The deep agent
selects which tool to call based on the user's request.

Example (deepagents):

```python
from deepagents import create_deep_agent
from dpo_agent import DocumentTools
from dpo_agent.integrations.langchain import make_dpo_tools

my_tools = DocumentTools(...)  # your 4 document tools
tools = make_dpo_tools(document_tools=my_tools)

agent = create_deep_agent(
    model="anthropic:claude-sonnet-5",
    tools=tools,
    system_prompt=(
        "You are a contract review assistant. You have access to "
        "9 tools: summarize, clause_classification, obligations, "
        "metadata, risk_score, dpo, redline_suggest, redline_apply, "
        "redline_negotiation. Use them to triage contracts and "
        "produce redlines."
    ),
)
result = agent.invoke({"messages": [...]})
```

The deep agent will call the appropriate tool(s) based on
the user's request. Each tool call is a full dpo-agent run
with its own prompt, tool loop, and output.

For the higher-level triage pipeline (5 tasks in sequence,
producing a single TriageReport), see `make_triage_tool()`.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

# LangChain tools are optional — the package can be used
# without deepagents. Import lazily.
try:
    from langchain.tools import tool
    _HAVE_LANGCHAIN = True
except ImportError:
    _HAVE_LANGCHAIN = False
    # Provide a no-op decorator so the module is importable
    # without langchain installed. Calling make_dpo_tools()
    # without langchain will raise a clear error.
    def tool(*args, **kwargs):  # type: ignore
        def decorator(func):
            return func
        return decorator


from ..agent import Agent
from ..pipeline import TriagePipeline, PipelineConfig
from ..tools import DocumentTools


def _format_output(text: str, task: str) -> str:
    """Format the agent's raw output for tool return.

    JSON-producing tasks (8 of 9) get pretty-printed JSON.
    The summarize task returns markdown and is returned as-is.
    """
    if task == "summarize":
        return text
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return text


def _build_common_input(
    document_id: str,
    defined_terms: Optional[dict[str, str]] = None,
    parties: Optional[list[dict[str, str]]] = None,
    jurisdiction_notes: str = "",
) -> dict[str, Any]:
    """Build the kwargs that all 9 task tools accept."""
    return {
        "document_id": document_id,
        "defined_terms": defined_terms,
        "parties": parties,
        "jurisdiction_notes": jurisdiction_notes,
    }


def make_dpo_tools(
    document_tools: DocumentTools,
    schema_input: bool = True,
) -> list[Callable]:
    """Build the 9 task tools for a deep agent.

    Args:
        document_tools: the caller's DocumentTools instance
            (wires the 4 document tools to a real document
            store). Shared across all 9 tools.
        schema_input: if True, the tools that need a schema
            (metadata, clause_classification, risk_score,
            redline_suggest, redline_apply, redline_negotiation)
            include a `schema_str` parameter. If False, the
            schema is omitted (the agent has to call the tool
            without one, which the underlying task will
            either accept as null or use a default for).

    Returns:
        A list of 9 LangChain tool functions, ready to pass
        to `create_deep_agent(tools=...)`.
    """
    if not _HAVE_LANGCHAIN:
        raise ImportError(
            "langchain is required for make_dpo_tools. "
            "Install with: pip install langchain"
        )

    # We need to close over document_tools and schema_input.
    # Each tool is defined separately so it can have a custom
    # Pydantic schema (langchain's @tool decorator infers the
    # schema from the function signature).

    # --- summarize ---
    @tool
    def summarize(
        document_id: str,
        jurisdiction_notes: str = "",
    ) -> str:
        """Generate a 4-section executive summary (TL;DR, Key
        Terms, Risks/Concerns, Open Questions) of a long
        document. Output is structured markdown. Use for
        pre-read briefings and portfolio triage.

        Args:
            document_id: the document to summarize (must exist
                in the document store).
            jurisdiction_notes: optional context about the
                audience and focus areas.
        """
        agent = Agent(tools=document_tools, task="summarize")
        result = agent.run(
            document_id=document_id,
            jurisdiction_notes=jurisdiction_notes,
        )
        return _format_output(result.review, "summarize")

    # --- clause_classification ---
    if schema_input:

        @tool
        def clause_classification(
            document_id: str,
            taxonomy: str,
            defined_terms: Optional[dict[str, str]] = None,
            parties: Optional[list[dict[str, str]]] = None,
        ) -> str:
            """Multi-label classification of every substantive
            clause in a contract against a caller-provided
            taxonomy. Each clause gets one or more labels with
            a confidence score. Use for CLM pre-load,
            portfolio analytics, training data.

            Args:
                document_id: the contract to classify.
                taxonomy: a JSON string of the label list, e.g.
                    '["indemnification", "limitation_of_liability"]'
                    or a rich list with descriptions.
                defined_terms, parties: optional context.
            """
            agent = Agent(tools=document_tools, task="clause_classification")
            result = agent.run(
                document_id=document_id,
                schema=taxonomy,
                defined_terms=defined_terms,
                parties=parties,
            )
            return _format_output(result.review, "clause_classification")
    else:

        @tool
        def clause_classification(document_id: str) -> str:
            """Multi-label classification of clauses (no
            taxonomy — uses default)."""
            agent = Agent(tools=document_tools, task="clause_classification")
            result = agent.run(document_id=document_id)
            return _format_output(result.review, "clause_classification")

    # --- obligations ---
    @tool
    def obligations(
        document_id: str,
        defined_terms: Optional[dict[str, str]] = None,
        parties: Optional[list[dict[str, str]]] = None,
        jurisdiction_notes: str = "",
    ) -> str:
        """Detect every binding obligation in a contract using
        the 5-field schema (obligor / obligee / action /
        deadline / condition). One row per binding
        commitment, not per clause. Use for CLM backfill and
        deadline tracking.

        Args:
            document_id: the contract to analyze.
            defined_terms, parties: optional context for
                resolving ambiguous names.
            jurisdiction_notes: optional context (e.g.
                "Provider is US, Customer has EU data subjects").
        """
        agent = Agent(tools=document_tools, task="obligations")
        result = agent.run(
            document_id=document_id,
            defined_terms=defined_terms,
            parties=parties,
            jurisdiction_notes=jurisdiction_notes,
        )
        return _format_output(result.review, "obligations")

    # --- metadata ---
    if schema_input:

        @tool
        def metadata(
            document_id: str,
            schema_str: str,
            known_metadata: Optional[dict[str, Any]] = None,
            source_hints: Optional[str] = None,
        ) -> str:
            """Extract structured metadata from a document
            against a caller-provided JSON schema. Output is
            JSON with per-field confidence scores, source
            citations, and open questions.

            Args:
                document_id: the document to extract from.
                schema_str: a JSON schema string describing the
                    fields to extract. (Renamed from `schema`
                    to avoid shadowing BaseModel.schema().)
                known_metadata: optional pre-known values
                    (from a CLM or prior extraction) for
                    verification.
                source_hints: optional hints (governing law,
                    document type) to save chunk reads.
            """
            agent = Agent(tools=document_tools, task="metadata")
            result = agent.run(
                document_id=document_id,
                schema=schema_str,
                known_metadata=known_metadata,
                source_hints=source_hints,
            )
            return _format_output(result.review, "metadata")
    else:

        @tool
        def metadata(document_id: str) -> str:
            """Extract metadata (no schema — uses default)."""
            agent = Agent(tools=document_tools, task="metadata")
            result = agent.run(document_id=document_id)
            return _format_output(result.review, "metadata")

    # --- risk_score ---
    if schema_input:

        @tool
        def risk_score(
            document_id: str,
            framework: str,
            contract_type: Optional[str] = None,
            counterparty: Optional[str] = None,
        ) -> str:
            """Multi-dimensional risk score (legal, financial,
            IP, data_protection, operational, reputational)
            against a caller-provided risk framework. Output
            includes a 1-10 aggregate score with confidence
            intervals, per-dimension breakdowns, top risks,
            and top wins. Use for portfolio triage and
            pre-execution review.

            Args:
                document_id: the contract to score.
                framework: a JSON string defining the risk
                    dimensions, weights, and rubrics.
                contract_type: optional hint (e.g. "MSA",
                    "DPA", "NDA") to inform dimension
                    importance.
                counterparty: optional counterparty profile
                    (tiebreaker, not a primary signal).
            """
            notes = (
                f"Contract type: {contract_type}. "
                f"Counterparty: {counterparty}."
                if contract_type or counterparty else ""
            )
            agent = Agent(tools=document_tools, task="risk_score")
            result = agent.run(
                document_id=document_id,
                schema=framework,
                jurisdiction_notes=notes,
            )
            return _format_output(result.review, "risk_score")
    else:

        @tool
        def risk_score(document_id: str) -> str:
            """Risk score (no framework — uses default 6-dim)."""
            agent = Agent(tools=document_tools, task="risk_score")
            result = agent.run(document_id=document_id)
            return _format_output(result.review, "risk_score")

    # --- dpo ---
    @tool
    def dpo(
        document_id: str,
        defined_terms: Optional[dict[str, str]] = None,
        parties: Optional[list[dict[str, str]]] = None,
        governing_law_hypothesis: Optional[str] = None,
        jurisdiction_notes: str = "",
    ) -> str:
        """Data Protection Officer contract review covering
        a 42-item GDPR/CCPA checklist. Output is a 4-section
        review (Triage, Findings, Obligations, Open
        Questions). Use for pre-execution GDPR review of
        vendor DPAs and MSAs.

        Args:
            document_id: the contract to review.
            defined_terms, parties: optional context.
            governing_law_hypothesis: optional pre-known
                governing law (saves chunk reads).
            jurisdiction_notes: optional context about
                jurisdictions, data subjects, etc.
        """
        agent = Agent(tools=document_tools, task="dpo")
        result = agent.run(
            document_id=document_id,
            defined_terms=defined_terms,
            parties=parties,
            governing_law_hypothesis=governing_law_hypothesis,
            jurisdiction_notes=jurisdiction_notes,
        )
        return _format_output(result.review, "dpo")

    # --- redline_suggest ---
    if schema_input:

        @tool
        def redline_suggest(
            document_id: str,
            playbook: str,
            firm_name: Optional[str] = None,
            counterparty_name: Optional[str] = None,
        ) -> str:
            """Compare a contract against a clause playbook
            and propose redlines for any clause that deviates.
            Output is a redline package with proposed_redlines,
            matching_clauses, and open_questions. Use before
            redline_apply.

            Args:
                document_id: the contract to analyze.
                playbook: a JSON string defining clause types
                    with preferred_language, fallback_language,
                    red_flags, and negotiable sub-points.
                firm_name, counterparty_name: optional names
                    to fill into redline suggestions.
            """
            notes = (
                f"Firm: {firm_name}. Counterparty: {counterparty_name}."
                if firm_name or counterparty_name else ""
            )
            agent = Agent(tools=document_tools, task="redline_suggest")
            result = agent.run(
                document_id=document_id,
                schema=playbook,
                jurisdiction_notes=notes,
            )
            return _format_output(result.review, "redline_suggest")
    else:

        @tool
        def redline_suggest(document_id: str) -> str:
            """Redline suggest (no playbook — uses minimal default)."""
            agent = Agent(tools=document_tools, task="redline_suggest")
            result = agent.run(document_id=document_id)
            return _format_output(result.review, "redline_suggest")

    # --- redline_apply ---
    if schema_input:

        @tool
        def redline_apply(
            document_id: str,
            redline_package: str,
            apply_mode: Optional[str] = "strict",
            track_changes: Optional[str] = "brackets",
        ) -> str:
            """Apply a redline package (from redline_suggest)
            to a source contract. Output is the redlined
            document + a change log + unapplied_redlines +
            suggested_additional_redlines. Use after
            redline_suggest.

            Args:
                document_id: the source contract.
                redline_package: a JSON string of the redline
                    package from redline_suggest.
                apply_mode: "strict" (default; exact match),
                    "fuzzy" (allow minor whitespace), or
                    "preview" (apply but mark every change).
                track_changes: "brackets" (default), "tracked"
                    (Word-style INSERT/DELETE markers), or
                    "clean" (no inline markers).
            """
            notes = (
                f"Apply mode: {apply_mode}. "
                f"Track changes: {track_changes}."
            )
            agent = Agent(tools=document_tools, task="redline_apply")
            result = agent.run(
                document_id=document_id,
                schema=redline_package,
                jurisdiction_notes=notes,
            )
            return _format_output(result.review, "redline_apply")
    else:

        @tool
        def redline_apply(document_id: str) -> str:
            """Redline apply (no package — uses default)."""
            agent = Agent(tools=document_tools, task="redline_apply")
            result = agent.run(document_id=document_id)
            return _format_output(result.review, "redline_apply")

    # --- redline_negotiation ---
    if schema_input:

        @tool
        def redline_negotiation(
            document_id: str,
            negotiation_playbook: str,
            deal_context: Optional[str] = None,
        ) -> str:
            """Position-by-position negotiation analysis.
            Compares the original contract, the firm's
            redlines, and the counterparty's counter-proposal.
            Recommends accept / counter / meet-in-middle /
            escalate for each disputed clause, calibrated to
            the firm's negotiation playbook. Use after
            redline_suggest + redline_apply.

            Args:
                document_id: the counterparty's counter-proposal
                    (or the original contract if no separate
                    counter is available).
                negotiation_playbook: a JSON string defining
                    per-clause-type strategy: preferred_outcome,
                    fallback_outcome, walk_away, BATNA,
                    concession_pattern.
                deal_context: optional deal context (deal value,
                    relationship, BATNA) to calibrate
                    recommendations.
            """
            notes = deal_context or ""
            agent = Agent(tools=document_tools, task="redline_negotiation")
            result = agent.run(
                document_id=document_id,
                schema=negotiation_playbook,
                jurisdiction_notes=notes,
            )
            return _format_output(result.review, "redline_negotiation")
    else:

        @tool
        def redline_negotiation(document_id: str) -> str:
            """Redline negotiation (no playbook — uses default)."""
            agent = Agent(tools=document_tools, task="redline_negotiation")
            result = agent.run(document_id=document_id)
            return _format_output(result.review, "redline_negotiation")

    return [
        summarize,
        clause_classification,
        obligations,
        metadata,
        risk_score,
        dpo,
        redline_suggest,
        redline_apply,
        redline_negotiation,
    ]


def make_triage_tool(document_tools: DocumentTools):
    """Build a single tool that runs the full triage pipeline.

    This is a higher-level alternative to the 9 individual
    tools: a single tool call runs the 5-stage pipeline
    (summarize → clause_classification → obligations →
    risk_score → dpo) and returns a unified TriageReport.

    Use this when the user wants "just triage this contract"
    rather than selecting individual tasks. The deep agent
    decides whether to call this or the individual tools
    based on the user's request.

    Returns:
        A LangChain tool function.
    """
    if not _HAVE_LANGCHAIN:
        raise ImportError(
            "langchain is required for make_triage_tool. "
            "Install with: pip install langchain"
        )

    @tool
    def triage_contract(
        document_id: str,
        jurisdiction_notes: str = "",
    ) -> str:
        """Run the full 5-task triage pipeline on a contract.
        Output is a unified TriageReport with a markdown
        triage document (summary, classifications, obligations,
        risk score, DPO findings) and per-stage JSON. Use for
        full intake triage in a single call.

        Args:
            document_id: the contract to triage.
            jurisdiction_notes: optional context (e.g.
                jurisdictions, data subjects).
        """
        pipeline = TriagePipeline(
            tools=document_tools,
            config=PipelineConfig(auto_confirm=True),
        )
        report = pipeline.run(
            document_id=document_id,
            jurisdiction_notes=jurisdiction_notes,
        )
        # Return both the markdown (human-readable) and a
        # summary of the JSON (machine-readable). The deep
        # agent can pick which to use.
        return (
            f"=== TRIAGE REPORT (markdown) ===\n"
            f"{report.markdown}\n\n"
            f"=== TRIAGE REPORT (JSON summary) ===\n"
            f"document_id: {report.document_id}\n"
            f"total_elapsed_seconds: {report.total_elapsed_seconds:.1f}\n"
            f"total_cost_estimate: ${report.total_cost_estimate:.2f}\n"
            f"stages: {len(report.stages)}\n"
        )

    return triage_contract
