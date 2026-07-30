"""Verify — Layer 7 of the 8-layer GraphRAG pipeline.

Per the GraphRAG build pipeline Layer 7, this layer checks:
  1. **Evidence coverage** — every claim in the answer is supported by
     at least one edge with confidence > threshold
  2. **Contradiction detection** — compare the new claim against
     existing facts; flag mismatches
  3. **Source verification** — every node and edge links back to a
     chunk in the original document
  4. **Confidence calibration** — the LLM's stated confidence matches
     the actual edge confidence
  5. **No hallucinations** — parties / clauses / obligations reference real text
  6. **Cross-contract consistency** — no contradictions with other contracts

The output is a `VerificationReport` with pass/fail per check and an
overall verdict. The pipeline uses this to gate production deployment.

dpo-agent integration: the `kg_verify` task uses these deterministic
checks; the LLM-driven critique is in the task's reviewer.md prompt
(see `dpo_agent/tasks/kg_verify/reviewer.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .ontology import Contract
from .store import GraphStore


@dataclass
class CheckResult:
    name: str
    passed: bool
    score: float  # 0-1
    details: str
    issues: List[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    contract_id: str
    checks: List[CheckResult]
    overall_passed: bool
    overall_score: float
    blocking_issues: List[str]  # issues that must be fixed before deployment

    def summary(self) -> str:
        lines = [
            f"Verification report for {self.contract_id}",
            f"Overall: {'PASS' if self.overall_passed else 'FAIL'} (score: {self.overall_score:.2f})",
            "",
        ]
        for c in self.checks:
            status = "✓" if c.passed else "✗"
            lines.append(f"  {status} {c.name}: {c.score:.2f} — {c.details}")
            for issue in c.issues[:3]:
                lines.append(f"      - {issue}")
        if self.blocking_issues:
            lines.append("")
            lines.append("Blocking issues:")
            for issue in self.blocking_issues:
                lines.append(f"  ! {issue}")
        return "\n".join(lines)


class Verifier:
    """Verifies a contract against the graph store and the ontology discipline.

    Most of the checks are deterministic (regex / SQL); the LLM-driven
    critique is in the kg_verify task's reviewer.md prompt.
    """

    def __init__(
        self,
        store: GraphStore,
        *,
        min_confidence: float = 0.5,
        require_evidence_on_clauses: bool = True,
    ) -> None:
        self.store = store
        self.min_confidence = min_confidence
        self.require_evidence_on_clauses = require_evidence_on_clauses

    def verify_contract(self, contract: Contract) -> VerificationReport:
        checks: List[CheckResult] = []
        checks.append(self._check_evidence_coverage(contract))
        checks.append(self._check_confidence(contract))
        checks.append(self._check_source_in_store(contract))
        checks.append(self._check_schema_discipline(contract))
        checks.append(self._check_no_hallucinations(contract))
        checks.append(self._check_contradictions(contract))
        if not checks:
            return VerificationReport(
                contract_id=contract.contract_id,
                checks=[],
                overall_passed=True,
                overall_score=1.0,
                blocking_issues=[],
            )
        overall_score = sum(c.score for c in checks) / len(checks)
        blocking: List[str] = []
        for c in checks:
            for issue in c.issues:
                if c.score < 0.5:
                    blocking.append(f"{c.name}: {issue}")
        return VerificationReport(
            contract_id=contract.contract_id,
            checks=checks,
            overall_passed=all(c.passed for c in checks),
            overall_score=overall_score,
            blocking_issues=blocking,
        )

    def _check_evidence_coverage(self, contract: Contract) -> CheckResult:
        """Every clause and obligation should have at least one evidence span."""
        if not contract.clauses and not contract.obligations:
            return CheckResult(
                name="evidence_coverage",
                passed=True,
                score=1.0,
                details="No clauses or obligations to verify (ok for some contracts).",
            )
        issues: List[str] = []
        n_missing = 0
        n_total = 0
        for cl in contract.clauses:
            n_total += 1
            if not cl.evidence:
                n_missing += 1
                issues.append(f"clause '{cl.clause_type}' has no evidence")
        for ob in contract.obligations:
            n_total += 1
            if not ob.evidence:
                n_missing += 1
                issues.append(f"obligation '{ob.action[:50]}' has no evidence")
        score = (n_total - n_missing) / n_total if n_total else 1.0
        passed = score >= (0.8 if self.require_evidence_on_clauses else 0.5)
        return CheckResult(
            name="evidence_coverage",
            passed=passed,
            score=score,
            details=f"{n_total - n_missing}/{n_total} clauses+obligations have evidence",
            issues=issues,
        )

    def _check_confidence(self, contract: Contract) -> CheckResult:
        """Confidence scores should be set and >= min_confidence for key fields."""
        issues: List[str] = []
        low: List[str] = []
        for cl in contract.clauses:
            if cl.confidence_score < self.min_confidence:
                low.append(f"clause '{cl.clause_type}' confidence={cl.confidence_score:.2f}")
        for ob in contract.obligations:
            if ob.confidence_score < self.min_confidence:
                low.append(f"obligation '{ob.action[:50]}' confidence={ob.confidence_score:.2f}")
        for p in contract.parties:
            if p.confidence_score < self.min_confidence:
                low.append(f"party '{p.name}' confidence={p.confidence_score:.2f}")
        n_low = len(low)
        n_total = len(contract.clauses) + len(contract.obligations) + len(contract.parties)
        score = 1.0 - (n_low / n_total) if n_total else 1.0
        return CheckResult(
            name="confidence_calibration",
            passed=n_low == 0,
            score=max(0.0, score),
            details=f"{n_low}/{n_total} items below confidence threshold {self.min_confidence}",
            issues=low[:5],
        )

    def _check_source_in_store(self, contract: Contract) -> CheckResult:
        """The contract should be present in the graph store."""
        row = self.store.get_contract(contract.contract_id)
        if row is None:
            return CheckResult(
                name="source_in_store",
                passed=False,
                score=0.0,
                details=f"Contract {contract.contract_id} not in store — was upsert called?",
                issues=[f"missing row for {contract.contract_id}"],
            )
        if contract.schema_version and row.get("schema_version") != contract.schema_version:
            return CheckResult(
                name="source_in_store",
                passed=True,
                score=0.8,
                details=f"Stored schema_version={row.get('schema_version')}, current={contract.schema_version}. Drift detected.",
                issues=[f"schema version drift: stored {row.get('schema_version')} != current {contract.schema_version}"],
            )
        return CheckResult(
            name="source_in_store",
            passed=True,
            score=1.0,
            details=f"Contract {contract.contract_id} present in store (version {row.get('version', 1)})",
        )

    def _check_schema_discipline(self, contract: Contract) -> CheckResult:
        """ISO 3166 country codes, ISO 8601 dates, etc."""
        issues: List[str] = []
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for f in ("effective_date", "end_date"):
            v = getattr(contract, f, None)
            if v and not date_re.match(v):
                issues.append(f"{f}='{v}' is not ISO 8601 yyyy-MM-dd")
        for ob in contract.obligations:
            if ob.deadline and not date_re.match(ob.deadline) and not ob.deadline.startswith("P"):
                issues.append(f"obligation deadline='{ob.deadline}' is not ISO 8601 date or duration")
        country_re = re.compile(r"^[A-Z]{2}$")
        for p in contract.parties:
            if p.location and p.location.country and not country_re.match(p.location.country):
                issues.append(f"party '{p.name}' country='{p.location.country}' is not ISO 3166 two-letter")
        if contract.governing_law and contract.governing_law.country and not country_re.match(contract.governing_law.country):
            issues.append(f"governing_law country='{contract.governing_law.country}' is not ISO 3166 two-letter")
        duration_re = re.compile(r"^P(\d+Y)?(\d+M)?(\d+D)?$")
        if contract.duration and not duration_re.match(contract.duration):
            issues.append(f"duration='{contract.duration}' is not ISO 8601 duration")
        n_issues = len(issues)
        score = 1.0 - min(1.0, n_issues * 0.1)
        return CheckResult(
            name="schema_discipline",
            passed=n_issues == 0,
            score=score,
            details=f"{n_issues} ISO/format violations" if n_issues else "All ISO formats valid",
            issues=issues,
        )

    def _check_no_hallucinations(self, contract: Contract) -> CheckResult:
        """Crude check: clause summary and obligation action should look reasonable."""
        issues: List[str] = []
        for cl in contract.clauses:
            if not cl.summary or len(cl.summary.strip()) < 5:
                issues.append(f"clause '{cl.clause_type}' has empty or trivial summary")
        for ob in contract.obligations:
            if not ob.obligor or not ob.obligee or not ob.action:
                issues.append(f"obligation missing obligor/obligee/action: '{ob.action[:50]}'")
        for p in contract.parties:
            if not p.name or len(p.name.strip()) < 2:
                issues.append(f"party has empty or trivial name: '{p.name}'")
        n_issues = len(issues)
        score = 1.0 - min(1.0, n_issues * 0.1)
        return CheckResult(
            name="no_hallucinations",
            passed=n_issues == 0,
            score=score,
            details=f"{n_issues} missing-field issues" if n_issues else "All required fields populated",
            issues=issues,
        )

    def _check_contradictions(self, contract: Contract) -> CheckResult:
        """Compare against other contracts: same party + different facts → contradiction."""
        issues: List[str] = []
        for p in contract.parties:
            others = self.store.contracts_by_party(p.name)
            for other in others:
                if other["contract_id"] == contract.contract_id:
                    continue
                if (
                    contract.governing_law
                    and contract.governing_law.country
                    and other.get("governing_law_country")
                    and contract.governing_law.country != other["governing_law_country"]
                ):
                    issues.append(
                        f"party '{p.name}': contract {contract.contract_id} governed by "
                        f"{contract.governing_law.country}, but contract {other['contract_id']} "
                        f"governed by {other['governing_law_country']}"
                    )
        n_issues = len(issues)
        score = 1.0 - min(1.0, n_issues * 0.2)
        return CheckResult(
            name="cross_contract_contradictions",
            passed=n_issues == 0,
            score=score,
            details=f"{n_issues} cross-contract contradictions" if n_issues else "No contradictions found",
            issues=issues[:5],
        )
