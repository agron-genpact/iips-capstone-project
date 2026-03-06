from __future__ import annotations

from datetime import datetime, timezone
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
        start_time = context.get("start_time", datetime.now(timezone.utc).isoformat())

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

    def _generate_posting_payload(self, invoice, match_result) -> PostingPayload:
        line_items = []
        for item in invoice.line_items:
            line_items.append(PostingLineItem(
                gl_account="",
                cost_center="",
                description=item.description,
                amount=item.amount,
                tax_code="",
                po_line_ref=item.po_line_ref or str(item.line_number),
            ))

        return PostingPayload(
            document_type="invoice",
            invoice_number=invoice.invoice_number,
            vendor_id=invoice.vendor_id,
            posting_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            currency=invoice.currency,
            total_amount=invoice.total_amount,
            tax_amount=invoice.tax_amount,
            po_number=invoice.po_number,
            payment_terms=invoice.payment_terms,
            line_items=line_items,
            status="ready",
            decision=DecisionAction.AUTO_POST,
        )

    def _generate_audit_log(self, decision: FinalDecision, context: dict) -> str:
        lines = [
            "# Audit Log",
            "",
            f"**Run ID:** {decision.run_id}",
            f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}",
            f"**Invoice:** {decision.invoice_number or 'N/A'}",
            f"**Vendor:** {decision.vendor_name or 'N/A'}",
            f"**Amount:** {decision.currency} {decision.total_amount or 'N/A'}",
            "",
            "---",
            "",
            "## Final Decision",
            "",
            f"- **Action:** {decision.decision.value}",
            f"- **Reason:** {decision.reason}",
            f"- **Risk Score:** {decision.risk_score}/10",
            f"- **Confidence:** {decision.confidence:.0%}",
            "",
            "---",
            "",
            "## Processing Trail",
            "",
        ]

        for entry in decision.audit_trail:
            lines.append(f"- {entry}")

        lines.extend(["", "---", "", "## Findings Summary", ""])

        if not decision.all_findings:
            lines.append("No findings recorded.")
        else:
            for f in decision.all_findings:
                icon = {"critical": "RED", "error": "ORANGE", "warning": "YELLOW", "info": "BLUE"}.get(f.severity.value, "WHITE")
                lines.append(f"- {icon} **[{f.severity.value.upper()}]** {f.title} "
                           f"(agent: {f.agent}, confidence: {f.confidence:.0%})")
                if f.evidence:
                    for e in f.evidence:
                        lines.append(f"  - Evidence: {e.source_file}"
                                   f"{f' - {e.field}' if e.field else ''}")

        lines.extend(["", "---", "",
                      f"*Generated by IIPS Orchestrator at {datetime.now(timezone.utc).isoformat()}*"])

        return "\n".join(lines)

    def _generate_metrics(
        self, context: dict, decision: FinalDecision,
        findings: list[Finding], start_time: str
    ) -> RunMetrics:
        """Generate processing metrics."""
        invoice = context.get("extracted_invoice")
        end_time = datetime.now(timezone.utc).isoformat()

        # Calculate duration
        try:
            start = datetime.fromisoformat(start_time)
            end = datetime.fromisoformat(end_time)
            duration = (end - start).total_seconds()
        except (ValueError, TypeError):
            duration = 0.0

        # Extraction confidence
        conf_scores = list(invoice.confidence_scores.values()) if invoice and invoice.confidence_scores else [0]
        avg_conf = sum(conf_scores) / len(conf_scores) if conf_scores else 0
        min_conf = min(conf_scores) if conf_scores else 0

        # Findings by severity/category
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for f in findings:
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
            by_category[f.category.value] = by_category.get(f.category.value, 0) + 1

        match_result = context.get("match_result")

        return RunMetrics(
            run_id=decision.run_id,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=round(duration, 2),
            documents_processed=len(context.get("context_packet", {}).documents)
            if hasattr(context.get("context_packet"), "documents") else 0,
            line_items_extracted=len(invoice.line_items) if invoice else 0,
            extraction_confidence_avg=round(avg_conf, 4),
            extraction_confidence_min=round(min_conf, 4),
            findings_total=len(findings),
            findings_by_severity=by_severity,
            findings_by_category=by_category,
            match_status=match_result.overall_status.value if match_result else "N/A",
            decision=decision.decision.value,
            exceptions_count=sum(1 for f in findings if f.severity in (Severity.CRITICAL, Severity.ERROR)),
            auto_posted=decision.decision == DecisionAction.AUTO_POST,
        )