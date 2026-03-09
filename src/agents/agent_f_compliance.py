"""
Agent F – Tax & Compliance Validation
Validates VAT IDs, tax rates, invoice structures, and regulatory requirements.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    DocumentType,
    EvidencePointer,
    ExceptionCategory,
    Finding,
    Severity,
)
from src.utils.file_utils import load_json, save_json


class ComplianceAgent(BaseAgent):
    name = "agent_f_compliance"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.set_default_evidence_from_context(context)
        self.log("Starting compliance validation")
        invoice = context.get("extracted_invoice")
        packet = context.get("context_packet")

        if not invoice:
            self.log("No extracted invoice – skipping compliance")
            return context

        # Load tax rules if available
        tax_rules = self._load_tax_rules(packet)

        if self.policy.tax_validation_enabled:
            # 1. VAT/Tax ID validation
            self._validate_tax_ids(invoice)

            # 2. Tax rate validation
            self._validate_tax_rates(invoice, tax_rules)

            # 3. Tax calculation verification
            self._verify_tax_calculations(invoice)

        # 4. Invoice structure compliance
        self._check_invoice_structure(invoice)

        # 5. Currency compliance
        self._check_currency_compliance(invoice)

        save_json(
            {"compliance_checked": True, "findings_count": len(self.findings)},
            self.run_dir / "compliance_result.json",
            mask_config=self.policy.privacy_mask_config,
        )

        self.log(f"Compliance check complete – {len(self.findings)} findings")
        return context

    def _load_tax_rules(self, packet) -> dict:
        """Load tax rules from bundle."""
        for doc in packet.documents:
            if doc.document_type == DocumentType.TAX_RULES:
                fpath = Path(doc.file_path)
                if fpath.exists() and fpath.suffix.lower() == ".json":
                    return load_json(fpath)
        return {}

    def _validate_tax_ids(self, invoice) -> None:
        """Validate vendor and buyer tax IDs."""
        if self.policy.get("compliance.require_valid_vat_id", True):
            if not invoice.vendor_tax_id:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.COMPLIANCE,
                    severity=Severity.WARNING,
                    confidence=1.0,
                    title="Missing vendor tax/VAT ID",
                    description="Vendor tax ID is required but not present on invoice",
                    recommendation="Request vendor tax ID before processing",
                ))
            elif not self._is_valid_tax_id_format(invoice.vendor_tax_id):
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.COMPLIANCE,
                    severity=Severity.WARNING,
                    confidence=0.8,
                    title="Invalid vendor tax ID format",
                    description=f"Vendor tax ID '{invoice.vendor_tax_id}' may have invalid format",
                    recommendation="Verify tax ID with vendor",
                ))

    def _is_valid_tax_id_format(self, tax_id: str) -> bool:
        """Basic tax ID format validation."""
        # EU VAT: 2 letter country code + 2-13 alphanumeric
        if re.match(r"^[A-Z]{2}\d{2,13}$", tax_id):
            return True
        # US EIN: XX-XXXXXXX
        if re.match(r"^\d{2}-\d{7}$", tax_id):
            return True
        # Generic: at least 5 alphanumeric characters
        if re.match(r"^[A-Za-z0-9\-]{5,}$", tax_id):
            return True
        return False

    def _validate_tax_rates(self, invoice, tax_rules: dict) -> None:
        """Validate tax rates against expected rates."""
        expected_rate = tax_rules.get("default_rate", self.policy.default_tax_rate)
        tolerance = tax_rules.get("rate_tolerance", self.policy.tax_rate_tolerance)

        # Check line-level tax rates
        for item in invoice.line_items:
            if item.tax_rate is not None:
                diff = abs(item.tax_rate - expected_rate)
                if diff > tolerance:
                    self.add_finding(Finding(
                        agent=self.name,
                        category=ExceptionCategory.TAX_MISMATCH,
                        severity=Severity.ERROR,
                        confidence=0.95,
                        title=f"Line {item.line_number}: tax rate mismatch",
                        description=(
                            f"Tax rate {item.tax_rate}% differs from expected "
                            f"{expected_rate}% (tolerance: ±{tolerance}%)"
                        ),
                        data={"line": item.line_number, "actual_rate": item.tax_rate,
                              "expected_rate": expected_rate},
                        recommendation="Verify applicable tax rate for this line item",
                    ))

        # Check overall tax amount ratio
        if invoice.subtotal and invoice.tax_amount and invoice.subtotal > 0:
            effective_rate = round(invoice.tax_amount / invoice.subtotal * 100, 2)
            diff = abs(effective_rate - expected_rate)
            if diff > tolerance:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.TAX_MISMATCH,
                    severity=Severity.WARNING,
                    confidence=0.9,
                    title="Overall effective tax rate mismatch",
                    description=(
                        f"Effective tax rate {effective_rate}% differs from "
                        f"expected {expected_rate}%"
                    ),
                    data={"effective_rate": effective_rate, "expected_rate": expected_rate},
                ))

    def _verify_tax_calculations(self, invoice) -> None:
        """Verify tax amount calculations."""
        for item in invoice.line_items:
            if item.tax_rate is not None and item.tax_amount is not None:
                expected_tax = round(item.amount * item.tax_rate / 100, 2)
                if abs(item.tax_amount - expected_tax) > 0.01:
                    self.add_finding(Finding(
                        agent=self.name,
                        category=ExceptionCategory.TAX_MISMATCH,
                        severity=Severity.WARNING,
                        confidence=1.0,
                        title=f"Line {item.line_number}: tax calculation error",
                        description=(
                            f"Tax amount ({item.tax_amount}) doesn't match "
                            f"amount ({item.amount}) × rate ({item.tax_rate}%) = {expected_tax}"
                        ),
                    ))

    def _check_invoice_structure(self, invoice) -> None:
        """Check invoice has required structural elements."""
        issues = []
        if not invoice.line_items:
            issues.append("No line items present")
        if not invoice.invoice_date:
            issues.append("Missing invoice date")
        if not invoice.vendor_name:
            issues.append("Missing vendor name")

        if issues:
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.COMPLIANCE,
                severity=Severity.WARNING,
                confidence=1.0,
                title="Invoice structure incomplete",
                description="Missing required elements: " + "; ".join(issues),
                recommendation="Ensure invoice meets minimum structural requirements",
            ))

    def _check_currency_compliance(self, invoice) -> None:
        """Check currency is allowed and consistent."""
        allowed = self.policy.allowed_currencies
        if invoice.currency not in allowed:
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.COMPLIANCE,
                severity=Severity.WARNING,
                confidence=1.0,
                title=f"Non-standard currency: {invoice.currency}",
                description=f"Currency '{invoice.currency}' is not in allowed list: {allowed}",
                recommendation="Verify currency handling and FX conversion",
            ))
