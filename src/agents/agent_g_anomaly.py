from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    EvidencePointer,
    ExceptionCategory,
    Finding,
    Severity,
)
from src.utils.file_utils import save_json


class AnomalyDetectionAgent(BaseAgent):
    name = "agent_g_anomaly"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.log("Starting anomaly detection")
        invoice = context.get("extracted_invoice")

        if not invoice:
            self.log("No extracted invoice – skipping anomaly detection")
            return context

        # 1. Duplicate detection
        self._check_duplicates(invoice, context)

        # 2. Amount anomalies
        self._check_amount_anomalies(invoice)

        # 3. Pattern anomalies
        self._check_pattern_anomalies(invoice, context)

        # 4. Vendor risk signals
        self._check_vendor_risk(invoice, context)

        save_json(
            {"anomaly_checked": True, "findings_count": len(self.findings)},
            self.run_dir / "anomaly_result.json",
        )

        self.log(f"Anomaly detection complete – {len(self.findings)} findings")
        return context

    def _check_duplicates(self, invoice, context: dict) -> None:
        """Check for duplicate invoices by comparing key attributes."""
        # Look for prior invoices in the run context or a history file
        history = context.get("invoice_history", [])

        for prior in history:
            similarity = self._compute_similarity(invoice, prior)
            if similarity >= self.policy.duplicate_similarity_threshold:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.DUPLICATE,
                    severity=Severity.CRITICAL,
                    confidence=similarity,
                    title="Potential duplicate invoice detected",
                    description=(
                        f"Invoice '{invoice.invoice_number}' is {similarity:.0%} similar to "
                        f"prior invoice '{prior.get('invoice_number', 'unknown')}' – "
                        f"same vendor, amount, or date"
                    ),
                    data={
                        "current_invoice": invoice.invoice_number,
                        "prior_invoice": prior.get("invoice_number"),
                        "similarity_score": similarity,
                    },
                    recommendation="Verify this is not a duplicate before processing",
                ))

        # Self-check: invoice number pattern suggesting duplicate
        if invoice.invoice_number:
            inv_num = invoice.invoice_number.strip()
            # Check if it looks like a re-submission (e.g., "INV-001-R1")
            if any(marker in inv_num.upper() for marker in ["-R", "-REV", "-DUP", "COPY"]):
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.DUPLICATE,
                    severity=Severity.WARNING,
                    confidence=0.7,
                    title="Invoice number suggests resubmission",
                    description=f"Invoice number '{inv_num}' contains revision/copy marker",
                    recommendation="Confirm this is an intentional resubmission",
                ))

    def _compute_similarity(self, invoice, prior: dict) -> float:
        """Compute similarity score between invoice and prior record."""
        from rapidfuzz import fuzz

        score = 0.0
        checks = 0

        # Invoice number match
        if invoice.invoice_number and prior.get("invoice_number"):
            if invoice.invoice_number == prior["invoice_number"]:
                score += 1.0
            else:
                score += fuzz.ratio(invoice.invoice_number, prior["invoice_number"]) / 100
            checks += 1

        # Vendor name match
        if invoice.vendor_name and prior.get("vendor_name"):
            name_score = fuzz.ratio(
                invoice.vendor_name.lower(),
                prior["vendor_name"].lower()
            ) / 100
            score += name_score
            checks += 1

        # Amount match
        if invoice.total_amount is not None and prior.get("total_amount") is not None:
            if invoice.total_amount == prior["total_amount"]:
                score += 1.0
            else:
                diff = abs(invoice.total_amount - prior["total_amount"])
                max_val = max(invoice.total_amount, prior["total_amount"], 1)
                score += max(0, 1 - diff / max_val)
            checks += 1

        # Date match
        if invoice.invoice_date and prior.get("invoice_date"):
            if invoice.invoice_date == prior["invoice_date"]:
                score += 1.0
            checks += 1

        return score / checks if checks > 0 else 0.0

    def _check_amount_anomalies(self, invoice) -> None:
        """Detect suspicious amount patterns."""
        amount = invoice.total_amount
        if amount is None:
            return

        # Round amount detection
        if self.policy.get("anomaly_detection.round_amount_flag", True):
            if amount >= 1000 and amount == round(amount, -2):
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.ANOMALY,
                    severity=Severity.INFO,
                    confidence=0.5,
                    title="Suspiciously round amount",
                    description=f"Invoice amount {amount} is a round number – may warrant review",
                    data={"amount": amount},
                ))

        # Just-under-threshold detection
        thresholds = [
            self.policy.auto_approve_max,
            self.policy.manager_approval_max,
        ]
        pct = self.policy.anomaly_just_under_pct

        for threshold in thresholds:
            lower_bound = threshold * (1 - pct / 100)
            if lower_bound <= amount < threshold:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.ANOMALY,
                    severity=Severity.WARNING,
                    confidence=0.7,
                    title=f"Amount just under approval threshold (${threshold:,.2f})",
                    description=(
                        f"Invoice amount ${amount:,.2f} is within {pct}% below "
                        f"the ${threshold:,.2f} approval threshold"
                    ),
                    data={"amount": amount, "threshold": threshold,
                          "distance_pct": round((threshold - amount) / threshold * 100, 2)},
                    recommendation="Review for potential threshold manipulation",
                ))

    def _check_pattern_anomalies(self, invoice, context: dict) -> None:
        """Detect unusual patterns in invoice data."""
        # Check for missing PO on high-value invoices
        if (invoice.total_amount and invoice.total_amount > self.policy.auto_approve_max
                and not invoice.po_number):
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.ANOMALY,
                severity=Severity.WARNING,
                confidence=0.8,
                title="High-value invoice without PO",
                description=(
                    f"Invoice for ${invoice.total_amount:,.2f} has no PO reference – "
                    f"above auto-approve threshold of ${self.policy.auto_approve_max:,.2f}"
                ),
                recommendation="Require PO or manager approval for high-value non-PO invoices",
            ))

        # Check for future-dated invoices
        if invoice.invoice_date:
            from datetime import datetime
            try:
                inv_date = datetime.fromisoformat(invoice.invoice_date.replace("/", "-"))
                if inv_date > datetime.utcnow():
                    self.add_finding(Finding(
                        agent=self.name,
                        category=ExceptionCategory.ANOMALY,
                        severity=Severity.WARNING,
                        confidence=0.9,
                        title="Future-dated invoice",
                        description=f"Invoice date {invoice.invoice_date} is in the future",
                    ))
            except (ValueError, TypeError):
                pass

    def _check_vendor_risk(self, invoice, context: dict) -> None:
        """Check for vendor-related risk indicators."""
        risk_indicators = []
        packet = context.get("context_packet")
        if packet:
            risk_indicators = packet.risk_indicators

        # Check context risk indicators
        for risk in risk_indicators:
            if "bank" in risk.lower() or "account" in risk.lower():
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.BANK_CHANGE,
                    severity=Severity.CRITICAL,
                    confidence=0.8,
                    title="Bank account risk detected in context",
                    description=f"Risk indicator: {risk}",
                    recommendation="Verify bank details through independent channel",
                ))