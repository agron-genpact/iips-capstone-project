from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    DecisionAction,
    ExceptionCategory,
    FinalDecision,
    Finding,
    PostingLineItem,
    PostingPayload,
    RunMetrics,
    Severity,
)
from src.utils.file_utils import save_json, save_markdown


class OrchestratorAgent(BaseAgent):
    name = "agent_i_orchestrator"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.log("Starting orchestration – final decision phase")
        run_id = context.get("run_id", "unknown")
        invoice = context.get("extracted_invoice")
        match_result = context.get("match_result")
        approval_packet = context.get("approval_packet")
        all_findings = context.get("all_findings", [])
        start_time = context.get("start_time", datetime.utcnow().isoformat())

        if not invoice:
            self.log("No invoice data – cannot orchestrate")
            return context

        # 1. Merge and deduplicate findings
        deduped = self._deduplicate_findings(all_findings)
        self.log(f"Deduplicated findings: {len(all_findings)} -> {len(deduped)}")

        # 2. Separate by severity
        critical = [f for f in deduped if f.severity == Severity.CRITICAL]
        errors = [f for f in deduped if f.severity == Severity.ERROR]
        warnings = [f for f in deduped if f.severity == Severity.WARNING]

        # 3. Compute risk score
        risk_score = self._compute_risk_score(deduped)

        # 4. Determine final decision
        decision, reason = self._make_decision(
            invoice, match_result, critical, errors, warnings, risk_score
        )

        # 5. Generate posting payload if applicable
        posting_payload = None
        if decision in (DecisionAction.AUTO_POST, DecisionAction.APPROVE_AND_POST):
            posting_payload = self._generate_posting_payload(invoice, match_result)
            save_json(posting_payload, self.run_dir / "posting_payload.json")
            self.log("Posting payload generated")

        # 6. Build final decision
        final = FinalDecision(
            run_id=run_id,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            total_amount=invoice.total_amount,
            currency=invoice.currency,
            decision=decision,
            reason=reason,
            all_findings=deduped,
            critical_findings=critical,
            risk_score=risk_score,
            confidence=self._compute_confidence(deduped, match_result),
            approval_packet=approval_packet,
            posting_payload=posting_payload,
        )

        # 7. Build audit trail
        audit_entries = context.get("audit_entries", [])
        audit_entries.append(f"[orchestrator] Final decision: {decision.value}")
        audit_entries.append(f"[orchestrator] Reason: {reason}")
        audit_entries.append(f"[orchestrator] Risk score: {risk_score:.2f}")
        audit_entries.append(f"[orchestrator] Findings: {len(deduped)} "
                           f"(C:{len(critical)} E:{len(errors)} W:{len(warnings)})")
        final.audit_trail = audit_entries

        save_json(final, self.run_dir / "final_decision.json")

        # 8. Generate audit log markdown
        audit_md = self._generate_audit_log(final, context)
        save_markdown(audit_md, self.run_dir / "audit_log.md")

        # 9. Generate metrics
        metrics = self._generate_metrics(context, final, deduped, start_time)
        save_json(metrics, self.run_dir / "metrics.json")

        context["final_decision"] = final
        self.log(f"Orchestration complete: {decision.value}")
        return context

    def _deduplicate_findings(self, findings: list[Finding]) -> list[Finding]:
        seen = set()
        deduped = []
        for f in findings:
            key = (f.category, f.title, f.agent)
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        # Sort by severity (critical first)
        severity_order = {Severity.CRITICAL: 0, Severity.ERROR: 1,
                         Severity.WARNING: 2, Severity.INFO: 3}
        deduped.sort(key=lambda x: severity_order.get(x.severity, 4))
        return deduped

    def _compute_risk_score(self, findings: list[Finding]) -> float:
        score = 0.0
        weights = {
            Severity.CRITICAL: 4.0,
            Severity.ERROR: 2.0,
            Severity.WARNING: 0.5,
            Severity.INFO: 0.1,
        }
        for f in findings:
            score += weights.get(f.severity, 0) * f.confidence
        return min(round(score, 2), 10.0)

    def _make_decision(
        self,
        invoice,
        match_result,
        critical: list[Finding],
        errors: list[Finding],
        warnings: list[Finding],
        risk_score: float,
    ) -> tuple[DecisionAction, str]:

        amount = invoice.total_amount or 0

        # Rule 1: Critical findings -> hold or reject
        if critical:
            has_fraud = any(
                f.category in (ExceptionCategory.BANK_CHANGE, ExceptionCategory.DUPLICATE)
                for f in critical
            )
            if has_fraud:
                return DecisionAction.HOLD, (
                    f"Held due to {len(critical)} critical finding(s) including "
                    f"potential fraud indicators. Manual investigation required."
                )
            return DecisionAction.REJECT, (
                f"Rejected due to {len(critical)} critical finding(s). "
                f"Issues must be resolved before resubmission."
            )

        # Rule 2: Errors -> route for approval
        if errors:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"{len(errors)} error(s) require review. "
                f"Routing to approver for resolution."
            )

        # Rule 3: High risk score
        if risk_score >= 5.0:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"Risk score {risk_score}/10 exceeds threshold. "
                f"Routing for manual review."
            )

        # Rule 4: Match status
        if match_result and not match_result.within_tolerance:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"Matching variance exceeds tolerance. "
                f"Total variance: {match_result.total_variance_pct}%"
            )

        # Rule 5: Amount-based routing
        if amount > self.policy.manager_approval_max:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"Amount ${amount:,.2f} exceeds manager approval threshold "
                f"(${self.policy.manager_approval_max:,.2f})"
            )

        # Rule 6: Warnings with amount above auto-approve
        if warnings and amount > self.policy.auto_approve_max:
            return DecisionAction.APPROVE_AND_POST, (
                f"Warnings present but within tolerance. "
                f"Amount ${amount:,.2f} requires acknowledgment."
            )

        # Rule 7: Clean invoice
        if not warnings:
            return DecisionAction.AUTO_POST, (
                f"No issues found. Invoice matches PO/GRN within tolerance. "
                f"Auto-posting approved."
            )

        # Rule 8: Minor warnings, low amount
        return DecisionAction.APPROVE_AND_POST, (
            f"Minor warnings present ({len(warnings)}) but amount "
            f"${amount:,.2f} is within auto-approval range."
        )

    def _compute_confidence(self, findings: list[Finding], match_result) -> float:
        if not findings:
            base = 1.0
        else:
            avg_conf = sum(f.confidence for f in findings) / len(findings)
            error_count = sum(1 for f in findings if f.severity in (Severity.CRITICAL, Severity.ERROR))
            base = max(0.1, avg_conf - error_count * 0.1)

        if match_result and match_result.within_tolerance:
            base = min(1.0, base + 0.1)

        return round(base, 2)
