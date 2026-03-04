from __future__ import annotations

from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    ApprovalPacket,
    DecisionAction,
    ExceptionCategory,
    Finding,
    Severity,
)
from src.utils.file_utils import save_json, save_markdown


class ExceptionTriageAgent(BaseAgent):
    name = "agent_h_exception"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.log("Starting exception triage")
        invoice = context.get("extracted_invoice")
        all_findings = context.get("all_findings", [])

        if not invoice:
            self.log("No extracted invoice – skipping triage")
            return context

        # Categorize findings by severity
        critical = [f for f in all_findings if f.severity == Severity.CRITICAL]
        errors = [f for f in all_findings if f.severity == Severity.ERROR]
        warnings = [f for f in all_findings if f.severity == Severity.WARNING]
        infos = [f for f in all_findings if f.severity == Severity.INFO]

        self.log(f"Triaging: {len(critical)} critical, {len(errors)} errors, "
                 f"{len(warnings)} warnings, {len(infos)} info")

        # Determine if approval is needed
        needs_approval = len(critical) > 0 or len(errors) > 0

        # Determine approver
        approver_role = self._determine_approver(invoice, all_findings)

        # Determine recommended action
        action = self._determine_action(invoice, critical, errors, warnings)

        # Build follow-up actions
        follow_ups = self._build_follow_ups(all_findings)

        # Build evidence summary
        evidence_summary = self._build_evidence_summary(all_findings)

        # Priority
        priority = "critical" if critical else "high" if errors else "normal"

        # Build approval packet
        packet = ApprovalPacket(
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            total_amount=invoice.total_amount,
            currency=invoice.currency,
            exceptions=all_findings,
            approval_required=needs_approval,
            approver_role=approver_role,
            approver_reason=self._build_approver_reason(critical, errors, warnings),
            priority=priority,
            recommended_action=action,
            follow_up_actions=follow_ups,
            evidence_summary=evidence_summary,
        )

        # Save artifacts
        save_json(packet, self.run_dir / "approval_packet.json")

        # Generate human-readable exceptions markdown
        exceptions_md = self._generate_exceptions_markdown(
            invoice, all_findings, packet
        )
        save_markdown(exceptions_md, self.run_dir / "exceptions.md")

        context["approval_packet"] = packet
        self.log(f"Exception triage complete – action: {action.value}")
        return context

    def _determine_approver(self, invoice, findings: list[Finding]) -> str:
        """Determine who should approve based on amount and findings."""
        amount = invoice.total_amount or 0

        # Critical findings always need director
        has_critical = any(f.severity == Severity.CRITICAL for f in findings)
        if has_critical:
            return "director"

        # Amount-based routing
        if amount > self.policy.manager_approval_max:
            return "director"
        elif amount > self.policy.auto_approve_max:
            return "manager"

        # Error findings need manager
        has_errors = any(f.severity == Severity.ERROR for f in findings)
        if has_errors:
            return "manager"

        return "auto"

    def _determine_action(
        self,
        invoice,
        critical: list[Finding],
        errors: list[Finding],
        warnings: list[Finding],
    ) -> DecisionAction:
        """Determine recommended processing action."""
        if critical:
            # Bank change or fraud -> hold
            has_fraud = any(
                f.category in (ExceptionCategory.BANK_CHANGE, ExceptionCategory.DUPLICATE)
                for f in critical
            )
            if has_fraud:
                return DecisionAction.HOLD
            return DecisionAction.REJECT

        if errors:
            return DecisionAction.ROUTE_FOR_APPROVAL

        if warnings:
            amount = invoice.total_amount or 0
            if amount <= self.policy.auto_approve_max:
                return DecisionAction.APPROVE_AND_POST
            return DecisionAction.ROUTE_FOR_APPROVAL

        return DecisionAction.AUTO_POST

    def _build_follow_ups(self, findings: list[Finding]) -> list[str]:
        """Build list of follow-up actions from findings."""
        follow_ups = []
        seen = set()

        for f in findings:
            if f.recommendation and f.recommendation not in seen:
                follow_ups.append(f.recommendation)
                seen.add(f.recommendation)

        return follow_ups

    def _build_evidence_summary(self, findings: list[Finding]) -> list[str]:
        """Build evidence summary for the approval packet."""
        summary = []
        for f in findings:
            if f.severity in (Severity.CRITICAL, Severity.ERROR):
                line = f"[{f.severity.value.upper()}] {f.title}"
                if f.evidence:
                    sources = [e.source_file for e in f.evidence if e.source_file]
                    if sources:
                        line += f" (evidence: {', '.join(sources[:2])})"
                summary.append(line)
        return summary

    def _build_approver_reason(
        self,
        critical: list[Finding],
        errors: list[Finding],
        warnings: list[Finding],
    ) -> str:
        """Build human-readable reason for approval routing."""
        parts = []
        if critical:
            parts.append(f"{len(critical)} critical issue(s)")
        if errors:
            parts.append(f"{len(errors)} error(s)")
        if warnings:
            parts.append(f"{len(warnings)} warning(s)")
        if not parts:
            return "No exceptions – auto-approval eligible"
        return "Requires review: " + ", ".join(parts)

    def _generate_exceptions_markdown(
        self, invoice, findings: list[Finding], packet: ApprovalPacket
    ) -> str:
        """Generate a human-friendly exceptions summary in Markdown."""
        lines = [
            "# Invoice Exception Summary",
            "",
            f"**Invoice:** {invoice.invoice_number or 'N/A'}",
            f"**Vendor:** {invoice.vendor_name or 'N/A'}",
            f"**Amount:** {invoice.currency} {invoice.total_amount or 'N/A'}",
            f"**Priority:** {packet.priority}",
            f"**Recommended Action:** {packet.recommended_action.value}",
            f"**Approver:** {packet.approver_role or 'auto'}",
            "",
            "---",
            "",
        ]

        if not findings:
            lines.append("No exceptions found. Invoice is ready for auto-posting.")
            return "\n".join(lines)

        # Group by severity
        for severity in [Severity.CRITICAL, Severity.ERROR, Severity.WARNING, Severity.INFO]:
            group = [f for f in findings if f.severity == severity]
            if not group:
                continue

            icon = {"critical": "!!!", "error": "!!", "warning": "!", "info": "i"}
            lines.append(f"## {severity.value.upper()} ({len(group)})")
            lines.append("")

            for f in group:
                lines.append(f"### [{icon.get(severity.value, '')}] {f.title}")
                lines.append(f"- **Category:** {f.category.value}")
                lines.append(f"- **Agent:** {f.agent}")
                lines.append(f"- **Confidence:** {f.confidence:.0%}")
                lines.append(f"- **Description:** {f.description}")
                if f.recommendation:
                    lines.append(f"- **Recommendation:** {f.recommendation}")
                if f.open_questions:
                    lines.append("- **Open Questions:**")
                    for q in f.open_questions:
                        lines.append(f"  - {q}")
                lines.append("")

        # Follow-up actions
        if packet.follow_up_actions:
            lines.append("## Next Actions")
            lines.append("")
            for i, action in enumerate(packet.follow_up_actions, 1):
                lines.append(f"{i}. {action}")
            lines.append("")

        return "\n".join(lines)