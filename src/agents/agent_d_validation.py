from __future__ import annotations

import re
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    EvidencePointer,
    ExceptionCategory,
    Finding,
    Severity,
)
from src.utils.file_utils import save_json


class ValidationAgent(BaseAgent):
    name = "agent_d_validation"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.set_default_evidence_from_context(context)
        self.log("Starting validation and normalization")
        invoice = context.get("extracted_invoice")

        if not invoice:
            self.log("No extracted invoice – skipping validation")
            return context

        # 1. Mandatory field checks
        self._check_mandatory_fields(invoice)

        # 2. Format normalization
        self._normalize_formats(invoice)

        # 3. Line item total reconciliation
        self._reconcile_totals(invoice)

        # 4. Line item internal consistency
        self._validate_line_items(invoice)

        # 5. Currency validation
        self._validate_currency(invoice)

        # 6. Date validation
        self._validate_dates(invoice)

        # Save validation result
        save_json(
            {"validated": True, "findings_count": len(self.findings)},
            self.run_dir / "validation_result.json",
            mask_config=self.policy.privacy_mask_config,
        )

        self.log(f"Validation complete – {len(self.findings)} findings")
        return context

    def _check_mandatory_fields(self, invoice) -> None:
        """Ensure all required fields are present."""
        mandatory = {
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date,
            "vendor_name": invoice.vendor_name,
            "total_amount": invoice.total_amount,
        }

        for field_name, value in mandatory.items():
            if not value:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.VALIDATION_ERROR,
                    severity=Severity.ERROR,
                    confidence=1.0,
                    title=f"Missing mandatory field: {field_name}",
                    description=f"Required field '{field_name}' is missing or empty",
                    recommendation=f"Provide {field_name} before processing",
                ))

    def _normalize_formats(self, invoice) -> None:
        """Normalize data formats for consistency."""
        # Normalize invoice number – strip whitespace
        if invoice.invoice_number:
            invoice.invoice_number = invoice.invoice_number.strip()

        # Normalize currency code to uppercase
        if invoice.currency:
            invoice.currency = invoice.currency.upper().strip()

        # Normalize amounts to 2 decimal places
        if invoice.total_amount is not None:
            invoice.total_amount = round(invoice.total_amount, 2)
        if invoice.subtotal is not None:
            invoice.subtotal = round(invoice.subtotal, 2)
        if invoice.tax_amount is not None:
            invoice.tax_amount = round(invoice.tax_amount, 2)

        for item in invoice.line_items:
            item.amount = round(item.amount, 2)
            item.unit_price = round(item.unit_price, 2)

    def _reconcile_totals(self, invoice) -> None:
        """Check that header total matches sum of line items."""
        if not invoice.line_items or invoice.total_amount is None:
            return

        line_total = sum(item.amount for item in invoice.line_items)
        line_total = round(line_total, 2)

        # Check subtotal if present
        if invoice.subtotal is not None:
            subtotal_diff = abs(invoice.subtotal - line_total)
            if subtotal_diff > 0.01:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.TOTAL_MISMATCH,
                    severity=Severity.ERROR,
                    confidence=1.0,
                    title="Subtotal does not match line items",
                    description=(
                        f"Header subtotal ({invoice.subtotal}) does not equal "
                        f"sum of line items ({line_total}). "
                        f"Difference: {subtotal_diff}"
                    ),
                    data={"header_subtotal": invoice.subtotal,
                          "computed_subtotal": line_total,
                          "difference": subtotal_diff},
                    recommendation="Verify line item amounts and subtotal",
                ))

        # Check total = subtotal + tax
        if invoice.subtotal is not None and invoice.tax_amount is not None:
            expected_total = round(invoice.subtotal + invoice.tax_amount, 2)
            total_diff = abs(invoice.total_amount - expected_total)
            if total_diff > 0.01:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.TOTAL_MISMATCH,
                    severity=Severity.ERROR,
                    confidence=1.0,
                    title="Total does not equal subtotal + tax",
                    description=(
                        f"Total ({invoice.total_amount}) != "
                        f"subtotal ({invoice.subtotal}) + tax ({invoice.tax_amount}) = {expected_total}"
                    ),
                    data={"total": invoice.total_amount,
                          "subtotal": invoice.subtotal,
                          "tax": invoice.tax_amount,
                          "expected_total": expected_total},
                ))
        else:
            # Check total vs line item sum (no tax info)
            tax_amount = invoice.tax_amount or 0
            expected_total = round(line_total + tax_amount, 2)
            total_diff = abs(invoice.total_amount - expected_total)
            if total_diff > 0.01:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.TOTAL_MISMATCH,
                    severity=Severity.WARNING,
                    confidence=0.9,
                    title="Total may not match line items",
                    description=(
                        f"Header total ({invoice.total_amount}) differs from "
                        f"computed total ({expected_total}). Difference: {total_diff}"
                    ),
                    data={"header_total": invoice.total_amount,
                          "computed_total": expected_total,
                          "difference": total_diff},
                ))

    def _validate_line_items(self, invoice) -> None:
        """Validate individual line items for internal consistency."""
        for item in invoice.line_items:
            # Check qty * price = amount
            expected = round(item.quantity * item.unit_price, 2)
            if abs(item.amount - expected) > 0.01:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.VALIDATION_ERROR,
                    severity=Severity.WARNING,
                    confidence=1.0,
                    title=f"Line {item.line_number}: amount mismatch",
                    description=(
                        f"Line {item.line_number}: qty ({item.quantity}) × "
                        f"price ({item.unit_price}) = {expected}, "
                        f"but amount is {item.amount}"
                    ),
                    data={"line_number": item.line_number,
                          "expected_amount": expected,
                          "actual_amount": item.amount},
                ))

            # Negative amounts
            if item.amount < 0:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.VALIDATION_ERROR,
                    severity=Severity.WARNING,
                    confidence=1.0,
                    title=f"Line {item.line_number}: negative amount",
                    description=f"Line item {item.line_number} has negative amount: {item.amount}",
                ))

            # Zero quantity
            if item.quantity == 0 and item.amount != 0:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.VALIDATION_ERROR,
                    severity=Severity.WARNING,
                    confidence=1.0,
                    title=f"Line {item.line_number}: zero quantity with non-zero amount",
                    description=f"Line {item.line_number} has zero quantity but amount {item.amount}",
                ))

    def _validate_currency(self, invoice) -> None:
        """Check currency is in allowed list."""
        allowed = self.policy.allowed_currencies
        if invoice.currency and invoice.currency not in allowed:
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.COMPLIANCE,
                severity=Severity.WARNING,
                confidence=1.0,
                title=f"Currency '{invoice.currency}' not in allowed list",
                description=f"Invoice currency {invoice.currency} is not in allowed currencies: {allowed}",
                recommendation="Verify currency and check FX handling",
            ))

    def _validate_dates(self, invoice) -> None:
        """Basic date format validation."""
        for field_name, value in [("invoice_date", invoice.invoice_date),
                                   ("due_date", invoice.due_date)]:
            if value:
                # Accept ISO format and common formats
                valid = bool(re.match(
                    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}",
                    value,
                ))
                if not valid:
                    self.add_finding(Finding(
                        agent=self.name,
                        category=ExceptionCategory.VALIDATION_ERROR,
                        severity=Severity.INFO,
                        confidence=0.8,
                        title=f"Non-standard date format: {field_name}",
                        description=f"Date '{value}' may need normalization",
                    ))
