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
from src.utils.file_utils import load_json, save_json, save_markdown


class OrchestratorAgent(BaseAgent):
    name = "agent_i_orchestrator"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.set_default_evidence_from_context(context)
        self.log("Starting orchestration – final decision phase")
        run_id = context.get("run_id", "unknown")
        invoice = context.get("extracted_invoice")
        match_result = context.get("match_result")
        approval_packet = context.get("approval_packet")
        all_findings = context.get("all_findings", [])
        self._strict = context.get("strict_reproducibility", False)
        start_time = context.get("start_time",
                                 "2000-01-01T00:00:00+00:00" if self._strict
                                 else datetime.now(timezone.utc).isoformat())

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
            invoice, match_result, deduped, critical, errors, warnings, risk_score
        )

        # 5. Generate posting payload for every run
        posting_payload = self._generate_posting_payload(invoice, match_result, decision)
        save_json(
            posting_payload,
            self.run_dir / "posting_payload.json",
            mask_config=self.policy.privacy_mask_config,
        )
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

        if approval_packet:
            approval_packet.recommended_action = decision
            save_json(
                approval_packet,
                self.run_dir / "approval_packet.json",
                mask_config=self.policy.privacy_mask_config,
            )

        # 7. Build audit trail
        audit_entries = context.get("audit_entries", [])
        audit_entries.append(f"[orchestrator] Final decision: {decision.value}")
        audit_entries.append(f"[orchestrator] Reason: {reason}")
        audit_entries.append(f"[orchestrator] Risk score: {risk_score:.2f}")
        audit_entries.append(f"[orchestrator] Findings: {len(deduped)} "
                           f"(C:{len(critical)} E:{len(errors)} W:{len(warnings)})")
        final.audit_trail = audit_entries

        save_json(
            final,
            self.run_dir / "final_decision.json",
            mask_config=self.policy.privacy_mask_config,
        )

        # 8. Generate audit log markdown
        audit_md = self._generate_audit_log(final, context)
        save_markdown(
            audit_md,
            self.run_dir / "audit_log.md",
            mask_config=self.policy.privacy_mask_config,
        )

        # 9. Generate metrics
        metrics = self._generate_metrics(context, final, deduped, start_time)
        save_json(
            metrics,
            self.run_dir / "metrics.json",
            mask_config=self.policy.privacy_mask_config,
        )

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
        all_findings: list[Finding],
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

        # Rule 2: No GRN must route for receipt confirmation.
        has_missing_grn = any(f.category == ExceptionCategory.MISSING_GRN for f in all_findings)
        if has_missing_grn and self.policy.require_grn_for_goods:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                "Missing GRN for goods invoice. Routed for receipt confirmation before posting."
            )

        # Rule 3: Non-PO routing policy.
        if not invoice.po_number:
            route = self.policy.non_po_routing
            if route in ("manager", "director", "approval", "route_for_approval"):
                return DecisionAction.ROUTE_FOR_APPROVAL, (
                    f"Non-PO invoice routed by policy (`matching.non_po_routing={route}`)."
                )
            if route == "hold":
                return DecisionAction.HOLD, (
                    "Non-PO invoice held by policy pending manual validation."
                )
            if route == "reject":
                return DecisionAction.REJECT, (
                    "Non-PO invoice rejected by policy."
                )
            if route == "manual_review":
                return DecisionAction.MANUAL_REVIEW, (
                    "Non-PO invoice routed for manual review by policy."
                )

        # Rule 4: Errors -> route for approval
        if errors:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"{len(errors)} error(s) require review. "
                f"Routing to approver for resolution."
            )

        # Rule 5: High risk score
        if risk_score >= 5.0:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"Risk score {risk_score}/10 exceeds threshold. "
                f"Routing for manual review."
            )

        # Rule 6: Match status
        if match_result and not match_result.within_tolerance:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"Matching variance exceeds tolerance. "
                f"Total variance: {match_result.total_variance_pct}%"
            )

        # Rule 7: Amount-based routing
        if amount > self.policy.manager_approval_max:
            return DecisionAction.ROUTE_FOR_APPROVAL, (
                f"Amount ${amount:,.2f} exceeds manager approval threshold "
                f"(${self.policy.manager_approval_max:,.2f})"
            )

        # Rule 8: Warnings with amount above auto-approve
        if warnings and amount > self.policy.auto_approve_max:
            return DecisionAction.APPROVE_AND_POST, (
                f"Warnings present but within tolerance. "
                f"Amount ${amount:,.2f} requires acknowledgment."
            )

        # Rule 9: Clean invoice
        if not warnings:
            return DecisionAction.AUTO_POST, (
                f"No issues found. Invoice matches PO/GRN within tolerance. "
                f"Auto-posting approved."
            )

        # Rule 10: Minor warnings, low amount
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

    def _generate_posting_payload(
        self,
        invoice,
        match_result,
        decision: DecisionAction,
    ) -> PostingPayload:
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
            posting_date="2000-01-01" if self._strict else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            currency=invoice.currency,
            total_amount=invoice.total_amount,
            tax_amount=invoice.tax_amount,
            po_number=invoice.po_number,
            payment_terms=invoice.payment_terms,
            line_items=line_items,
            status=self._posting_status_for_decision(decision),
            decision=decision,
        )

    def _posting_status_for_decision(self, decision: DecisionAction) -> str:
        mapping = {
            DecisionAction.AUTO_POST: "ready",
            DecisionAction.APPROVE_AND_POST: "approved_pending_post",
            DecisionAction.ROUTE_FOR_APPROVAL: "pending_approval",
            DecisionAction.HOLD: "on_hold",
            DecisionAction.REJECT: "rejected",
            DecisionAction.MANUAL_REVIEW: "manual_review",
        }
        return mapping.get(decision, "pending_approval")

    def _generate_audit_log(self, decision: FinalDecision, context: dict) -> str:
        lines = [
            "# Audit Log",
            "",
            f"**Run ID:** {decision.run_id}",
            f"**Timestamp:** {'2000-01-01T00:00:00+00:00' if self._strict else datetime.now(timezone.utc).isoformat()}",
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
                      f"*Generated by IIPS Orchestrator at {'2000-01-01T00:00:00+00:00' if self._strict else datetime.now(timezone.utc).isoformat()}*"])

        return "\n".join(lines)

    def _generate_metrics(
        self, context: dict, decision: FinalDecision,
        findings: list[Finding], start_time: str
    ) -> RunMetrics:
        """Generate processing metrics."""
        invoice = context.get("extracted_invoice")
        strict = context.get("strict_reproducibility", False)
        end_time = "2000-01-01T00:00:00+00:00" if strict else datetime.now(timezone.utc).isoformat()

        # Calculate duration
        if strict:
            duration = 0.0
        else:
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
        extraction_field_accuracy = self._compute_extraction_field_accuracy(context, invoice)

        # Findings by severity/category
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for f in findings:
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
            by_category[f.category.value] = by_category.get(f.category.value, 0) + 1

        match_result = context.get("match_result")
        matching_lines_total = 0
        matching_lines_matched = 0
        matching_line_precision = 0.0
        if match_result and getattr(match_result, "line_matches", None):
            matching_lines_total = len(match_result.line_matches)
            matching_lines_matched = sum(
                1 for lm in match_result.line_matches
                if getattr(getattr(lm, "status", None), "value", "") == "matched"
            )
            if matching_lines_total > 0:
                matching_line_precision = matching_lines_matched / matching_lines_total

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
            extraction_field_accuracy=round(extraction_field_accuracy, 4),
            findings_total=len(findings),
            findings_by_severity=by_severity,
            findings_by_category=by_category,
            match_status=match_result.overall_status.value if match_result else "N/A",
            matching_line_precision=round(matching_line_precision, 4),
            matching_lines_total=matching_lines_total,
            matching_lines_matched=matching_lines_matched,
            decision=decision.decision.value,
            exceptions_count=sum(1 for f in findings if f.severity in (Severity.CRITICAL, Severity.ERROR)),
            auto_posted=decision.decision == DecisionAction.AUTO_POST,
        )

    def _compute_extraction_field_accuracy(self, context: dict, invoice) -> float:
        """Compute header field accuracy when ground truth invoice JSON exists."""
        if not invoice:
            return 0.0

        packet = context.get("context_packet")
        documents = getattr(packet, "documents", []) if packet else []
        source_invoice = None
        for doc in documents:
            if getattr(getattr(doc, "document_type", None), "value", "") != "invoice":
                continue
            source_path = Path(doc.file_path)
            if source_path.suffix.lower() != ".json" or not source_path.exists():
                continue
            try:
                payload = load_json(source_path)
            except Exception:
                continue
            if isinstance(payload, dict):
                source_invoice = payload
                break

        if not source_invoice:
            return 0.0

        fields = ("invoice_number", "invoice_date", "vendor_name", "po_number", "currency", "total_amount")
        compared = 0
        matched = 0
        for field in fields:
            expected = source_invoice.get(field)
            if expected is None or expected == "":
                continue
            compared += 1
            actual = getattr(invoice, field, None)
            if field == "total_amount":
                try:
                    if abs(float(actual) - float(expected)) <= 0.01:
                        matched += 1
                except (TypeError, ValueError):
                    continue
                continue
            if field == "currency":
                if str(actual).strip().upper() == str(expected).strip().upper():
                    matched += 1
                continue
            if str(actual).strip() == str(expected).strip():
                matched += 1

        if compared == 0:
            return 0.0
        return matched / compared
