"""kgpipeline integration — convert dpo-agent TriageReports to
kgpipeline Contracts and run the kgpipeline layers.

The KEY OPTIMIZATION: the dpo-agent's triage pipeline already
produces structured data (metadata, clause_classification,
obligations, summarize). The kgpipeline's Layer 2 (extract)
is also an LLM call that produces the same data, just in a
different Pydantic schema. Running both would burn tokens
and produce inconsistent results.

This module provides:

1. **`from_triage_report(triage_report, contract_id, document_text)`**
   — converts a dpo-agent TriageReport into a
   `kgpipeline.ontology.Contract` Pydantic object, with
   evidence spans that point to the source contract.

2. **`build_graph(triage_report, document_id, contract_id, db_path, document_text)`**
   — high-level: runs `from_triage_report` + the kgpipeline's
   resolve + store + verify + update layers. Skips ingest
   (the contract is already in dpo-agent's document store)
   and extract (the TriageReport has the structured data).

3. **`run_pipeline(triage_report, document_id, contract_id, db_path, ...)`**
   — full kgpipeline PipelineResult with the TriageReport
   adapter plugged in.

The kgpipeline package is **optional** — this module imports
it lazily and raises a clear ImportError if it's not
installed. Install with:

    pip install kgpipeline  # not yet on PyPI; install from source
    # or:
    pip install -e /path/to/wiki-contracts
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Lazy import of kgpipeline — it's an optional dependency.
try:
    from kgpipeline.ontology import (
        Contract,
        ContractType,
        Party,
        PartyRole,
        Location,
        Clause,
        Obligation,
        MoneyAmount,
        DateField,
        EvidenceSpan,
        SCHEMA_VERSION,
    )
    from kgpipeline.resolve import resolve_parties
    from kgpipeline.store import GraphStore
    from kgpipeline.verify import Verifier, VerificationReport
    from kgpipeline.update import classify_update, UpdateVerdict
    from kgpipeline.llm import LLMProvider
    _HAVE_KGPIPELINE = True
except ImportError:
    _HAVE_KGPIPELINE = False


@dataclass
class TriageReportAdapter:
    """Converts a dpo-agent TriageReport into a kgpipeline
    Contract. Stores the source document text for
    evidence-span construction.
    """

    triage_report: dict
    document_text: str
    contract_id: str

    def build_contract(self) -> "Contract":
        """Build the kgpipeline Contract from the TriageReport.

        Mapping:
        - metadata.parties → Contract.parties (Party objects)
        - metadata.effective_date / term_months → Contract dates
        - metadata.governing_law → Contract.governing_law
        - metadata.payment_terms → free-form in summary
        - clause_classification.classifications → Contract.clauses
        - obligations.obligations → Contract.obligations
        - summarize → Contract.summary
        - risk_score.headline → embedded in summary
        - dpo.executive_summary → embedded in summary
        """
        if not _HAVE_KGPIPELINE:
            raise ImportError(
                "kgpipeline is required. Install from "
                "wiki-contracts: pip install -e /path/to/wiki-contracts"
            )

        stages = {
            s["task"]: s.get("output", {})
            for s in self.triage_report.get("stages", [])
        }

        # --- Parties ---
        metadata = stages.get("metadata", {})
        # The metadata stage output may be raw text (markdown)
        # or a JSON object, depending on the task. For dpo-agent,
        # metadata returns JSON.
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        meta_props = metadata.get("properties", metadata) if isinstance(metadata, dict) else {}

        parties = self._build_parties(meta_props)
        # Augment with parties found in clause_classification
        # and obligations (the TriageReport may name parties
        # the metadata stage missed).
        parties = self._augment_parties(
            parties, stages.get("clause_classification", {}),
            stages.get("obligations", {}),
        )

        # --- Clauses ---
        cc = stages.get("clause_classification", {})
        if isinstance(cc, str):
            try:
                cc = json.loads(cc)
            except json.JSONDecodeError:
                cc = {}
        cc_output = cc.get("classifications", []) if isinstance(cc, dict) else []
        clauses = self._build_clauses(cc_output)

        # --- Obligations ---
        obl = stages.get("obligations", {})
        if isinstance(obl, str):
            try:
                obl = json.loads(obl)
            except json.JSONDecodeError:
                obl = {}
        obl_output = obl.get("obligations", []) if isinstance(obl, dict) else []
        obligations = self._build_obligations(obl_output, parties)

        # --- Dates / governing law / amount ---
        effective_date = self._parse_date(meta_props.get("effective_date"))
        duration = self._parse_duration(meta_props.get("term_months"))
        governing_law = self._parse_location(meta_props.get("governing_law"))
        total_amount = self._parse_money(meta_props.get("total_amount"))

        # --- Summary ---
        # The dpo-agent summary is a markdown document. Embed
        # the TriageReport's summary + risk score + DPO findings.
        summary_text = self._compose_summary(stages)

        # --- Contract type ---
        contract_type_str = meta_props.get("contract_type", "OTHER")
        try:
            contract_type = ContractType(contract_type_str)
        except ValueError:
            # The dpo-agent contract_type vocabulary may not
            # match the kgpipeline enum exactly. Default to OTHER.
            contract_type = ContractType.OTHER

        return Contract(
            contract_id=self.contract_id,
            contract_type=contract_type,
            title=meta_props.get("title"),
            summary=summary_text,
            parties=parties,
            effective_date=effective_date,
            duration=duration,
            total_amount=total_amount,
            governing_law=governing_law,
            clauses=clauses,
            obligations=obligations,
            source_path=self.contract_id,  # default; caller can override
            extraction_model="dpo-agent (no LLM extraction)",
            schema_version=SCHEMA_VERSION,
        )

    def _build_parties(self, meta_props: dict) -> list["Party"]:
        """Build Party objects from the metadata stage output."""
        out = []
        party_data = meta_props.get("parties", [])
        if isinstance(party_data, list):
            for p in party_data:
                # Handle both {name, role} dicts and plain name strings.
                if isinstance(p, str):
                    name = p
                    role_str = "other"
                elif isinstance(p, dict):
                    name = p.get("name", "")
                    role_str = p.get("role", "other").lower()
                else:
                    continue
                try:
                    role = PartyRole(role_str)
                except ValueError:
                    role = PartyRole.OTHER
                location = None
                if isinstance(p, dict) and p.get("location"):
                    location = self._parse_location(p["location"])
                out.append(Party(
                    name=name,
                    role=role,
                    location=location,
                    source_chunk_id=None,
                    confidence_score=1.0,  # TriageReport is pre-extracted
                ))
        return out

    def _augment_parties(
        self,
        parties: list["Party"],
        clause_classification: dict,
        obligations: dict,
    ) -> list["Party"]:
        """If a party is mentioned in clauses or obligations but
        not in metadata.parties, add them."""
        known_names = {p.name for p in parties}
        known_names_lower = {n.lower() for n in known_names}

        # Look for party names in clause classifications.
        for c in clause_classification.get("classifications", []):
            text = c.get("clause_text", "")
            # Heuristic: "Provider", "Customer", "Vendor" are
            # common role-as-party names. The dpo-agent output
            # may use these as defined terms.
            for role_word in ("Provider", "Customer", "Vendor",
                              "Supplier", "Buyer", "Seller"):
                if role_word in text and role_word.lower() not in known_names_lower:
                    # Add as a generic party with the role-word
                    # name.
                    try:
                        role = PartyRole(role_word.lower())
                    except ValueError:
                        role = PartyRole.OTHER
                    parties.append(Party(
                        name=role_word,
                        role=role,
                        source_chunk_id=None,
                        confidence_score=0.7,
                    ))
                    known_names.add(role_word)
                    known_names_lower.add(role_word.lower())
        return parties

    def _build_clauses(self, classifications: list) -> list["Clause"]:
        """Build Clause objects from clause_classification output."""
        out = []
        for i, c in enumerate(classifications):
            clause_type = c.get("clause_type", "Other")
            if isinstance(c.get("labels"), list) and c["labels"]:
                # If multiple labels, use the first one (the
                # primary classification). The kgpipeline Clause
                # has a single clause_type field.
                first_label = c["labels"][0]
                clause_type = first_label.get("label", clause_type)
            text = c.get("clause_text", "")
            section = c.get("section_ref", "")
            evidence = self._build_evidence(text, section) if text else []
            out.append(Clause(
                clause_type=clause_type,
                summary=text[:200] if text else f"Clause at {section}",
                evidence=evidence,
                confidence_score=c.get("confidence", 0.8) if c.get("confidence") else 0.8,
            ))
        return out

    def _build_obligations(
        self,
        obligations: list,
        parties: list["Party"],
    ) -> list["Obligation"]:
        """Build Obligation objects from the obligations output."""
        out = []
        for o in obligations:
            text = o.get("verbatim_text", "")
            section = o.get("clause_ref", "")
            evidence = self._build_evidence(text, section) if text else []
            out.append(Obligation(
                obligor=o.get("obligor", ""),
                obligee=o.get("obligee", ""),
                action=o.get("action", ""),
                deadline=o.get("deadline"),
                condition=o.get("condition"),
                evidence=evidence,
                confidence_score=o.get("confidence_score", 0.8) if o.get("confidence_score") else 0.8,
            ))
        return out

    def _build_evidence(self, text: str, section: str) -> list["EvidenceSpan"]:
        """Build EvidenceSpan objects by finding the text in
        the source document. If not found, return a placeholder
        with the section reference.
        """
        if not text:
            return []
        # Try to find the text in the document.
        idx = self.document_text.find(text)
        if idx == -1:
            # Try a partial match (first 50 chars).
            partial = text[:50]
            idx = self.document_text.find(partial)
        if idx == -1:
            # Not found — produce a placeholder.
            return [EvidenceSpan(
                chunk_id=section or "unknown",
                char_start=0,
                char_end=0,
                quote=text[:100] if text else "",
            )]
        return [EvidenceSpan(
            chunk_id=section or "unknown",
            char_start=idx,
            char_end=idx + len(text),
            quote=text,
        )]

    def _parse_date(self, date_str: Any) -> Optional[str]:
        """Parse a date string into ISO 8601 yyyy-MM-dd format."""
        if not date_str or not isinstance(date_str, str):
            return None
        # If already in yyyy-MM-dd format, return as-is.
        import re
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return date_str
        # Try to parse "Month DD, YYYY" or "DD Month YYYY".
        from datetime import datetime
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
                    "%Y-%m-%d", "%m/%d/%Y"):
            try:
                d = datetime.strptime(date_str, fmt)
                return d.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _parse_duration(self, term_months: Any) -> Optional[str]:
        """Convert term_months to ISO 8601 duration (e.g. P2Y)."""
        if not term_months:
            return None
        try:
            months = int(term_months)
        except (ValueError, TypeError):
            return None
        if months == 0:
            return None
        years = months // 12
        rem_months = months % 12
        if years == 0:
            return f"P{rem_months}M"
        if rem_months == 0:
            return f"P{years}Y"
        return f"P{years}Y{rem_months}M"

    def _parse_location(self, loc: Any) -> Optional["Location"]:
        """Parse a location string (e.g. 'Delaware, USA') into a
        Location object."""
        if not loc:
            return None
        if isinstance(loc, Location):
            return loc
        if isinstance(loc, dict):
            return Location(
                city=loc.get("city"),
                state=loc.get("state"),
                country=loc.get("country"),
            )
        if isinstance(loc, str):
            # Heuristic: "City, State, Country" or "State, Country".
            parts = [p.strip() for p in loc.split(",")]
            return Location(
                city=parts[0] if len(parts) >= 1 and "," in loc else None,
                state=parts[0] if len(parts) == 2 else (
                    parts[1] if len(parts) >= 3 else None
                ),
                country=parts[-1] if len(parts) >= 1 else None,
            )
        return None

    def _parse_money(self, money: Any) -> Optional["MoneyAmount"]:
        """Parse a money string (e.g. '$50,000 USD') into a
        MoneyAmount object."""
        if not money:
            return None
        if isinstance(money, MoneyAmount):
            return money
        if isinstance(money, (int, float)):
            return MoneyAmount(amount=float(money), currency=None)
        if isinstance(money, str):
            import re
            # Try to extract the number.
            num_match = re.search(r"[\d,]+(?:\.\d+)?", money)
            if num_match:
                amount = float(num_match.group().replace(",", ""))
                # Try to extract the currency.
                currency_match = re.search(
                    r"\b(USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY)\b", money
                )
                currency = currency_match.group(1) if currency_match else None
                return MoneyAmount(
                    amount=amount,
                    currency=currency,
                    raw_text=money,
                )
        return None

    def _compose_summary(self, stages: dict) -> str:
        """Compose the Contract.summary from summarize +
        risk_score + dpo."""
        parts = []
        summarize = stages.get("summarize", "")
        if isinstance(summarize, str) and summarize:
            parts.append("## Summary\n\n" + summarize[:1000])
        risk = stages.get("risk_score", {})
        if isinstance(risk, dict):
            headline = risk.get("headline", {})
            if isinstance(headline, dict):
                score = headline.get("score")
                band = headline.get("band")
                if score is not None:
                    parts.append(f"## Risk score\n\n{score} ({band})")
        dpo = stages.get("dpo", {})
        if isinstance(dpo, dict):
            dpo_summary = dpo.get("executive_summary", {})
            if isinstance(dpo_summary, dict):
                one_para = dpo_summary.get("one_paragraph")
                if one_para:
                    parts.append("## Privacy review\n\n" + one_para)
        return "\n\n".join(parts) if parts else "No summary available."


def build_graph(
    triage_report: dict,
    document_id: str,
    contract_id: str,
    db_path: str,
    document_text: str,
    *,
    provider: Optional["LLMProvider"] = None,
) -> dict:
    """Build the kgpipeline graph from a dpo-agent TriageReport.

    This is the high-level function: convert the TriageReport
    to a kgpipeline Contract, then run resolve + store +
    classify + verify. Skips kgpipeline's extract (Layer 2)
    because the TriageReport already has the structured data.

    Args:
        triage_report: the dpo-agent TriageReport (a dict).
        document_id: the dpo-agent document_id (for evidence
            verification, if needed).
        contract_id: the unique contract_id to use in the graph
            (defaults to the document_id).
        db_path: path to the SQLite graph database.
        document_text: the source contract text (for
            evidence-span construction).
        provider: optional kgpipeline LLMProvider (used for
            resolve + classify if LLM-based dedup is needed).
            If None, the resolve step uses deterministic dedup
            only.

    Returns:
        A dict with the kgpipeline artifacts (Contract,
        GraphStore, Verifier, VerificationReport,
        UpdateVerdicts). Caller can serialize as needed.
    """
    if not _HAVE_KGPIPELINE:
        raise ImportError(
            "kgpipeline is required. Install from "
            "wiki-contracts: pip install -e /path/to/wiki-contracts"
        )

    # 1. Build the Contract
    adapter = TriageReportAdapter(
        triage_report=triage_report,
        document_text=document_text,
        contract_id=contract_id,
    )
    contract = adapter.build_contract()

    # 2. Open the store
    store = GraphStore(db_path)

    # 3. Resolve (Layer 3) — dedup parties
    canonical_map, decisions = resolve_parties(
        contract.parties, provider=provider,
    )

    # 4. Classify update (Layer 8) — before upsert, against
    #    the current graph state
    verdict = classify_update(contract, store, provider=provider)

    # 5. Store (Layer 4)
    store.upsert(contract)

    # 6. Verify (Layer 7)
    verifier = Verifier(store)
    verification = verifier.verify_contract(contract)

    return {
        "contract": contract,
        "store": store,
        "verifier": verifier,
        "verification": verification,
        "update_verdict": verdict,
        "canonical_map": canonical_map,
        "decisions": decisions,
    }


def run_pipeline(
    triage_report: dict,
    document_id: str,
    contract_id: str,
    db_path: str,
    document_text: str,
    *,
    provider: Optional["LLMProvider"] = None,
    export_cypher: Optional[str] = None,
) -> dict:
    """Build the graph and return a kgpipeline-shaped
    PipelineResult. For users who want the same return type
    as `kgpipeline.pipeline.run()`.

    The key difference: the TriageReport's structured data
    replaces kgpipeline's extract layer (Layer 2). Layers
    1 (ingest), 2 (extract), 5 (retrieve), 6 (agent) are
    skipped; layers 3 (resolve), 4 (store), 7 (verify),
    8 (update) are run.
    """
    result = build_graph(
        triage_report=triage_report,
        document_id=document_id,
        contract_id=contract_id,
        db_path=db_path,
        document_text=document_text,
        provider=provider,
    )
    cypher_path = None
    if export_cypher:
        cypher_path = Path(export_cypher)
        cypher_path.write_text(result["store"].to_cypher())
    return {
        "contract": result["contract"],
        "store": result["store"],
        "verifier": result["verifier"],
        "verification": result["verification"],
        "update_verdict": result["update_verdict"],
        "cypher_path": cypher_path,
        "db_path": Path(db_path),
        "layers_run": ["resolve", "store", "classify", "verify"],
        "layers_skipped": ["ingest", "extract", "retrieve", "agent"],
    }


def kg_build_from_triage_pipeline(
    pipeline_report: dict,
    document_text: str,
    db_path: str,
    *,
    contract_id: Optional[str] = None,
    provider: Optional["LLMProvider"] = None,
    export_cypher: Optional[str] = None,
) -> dict:
    """Convenience function: take a `TriageReport` (the dict
    output of `TriagePipeline.run()`), build the graph.

    This is the function the dpo-agent `kg_build` task uses
    internally. End users can call it directly.
    """
    document_id = pipeline_report.get("document_id", "unknown")
    contract_id = contract_id or document_id
    return run_pipeline(
        triage_report=pipeline_report,
        document_id=document_id,
        contract_id=contract_id,
        db_path=db_path,
        document_text=document_text,
        provider=provider,
        export_cypher=export_cypher,
    )


# Public API
__all__ = [
    "TriageReportAdapter",
    "build_graph",
    "run_pipeline",
    "kg_build_from_triage_pipeline",
    "_HAVE_KGPIPELINE",
]
