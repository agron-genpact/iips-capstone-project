from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    DocumentType,
    EvidencePointer,
    ExceptionCategory,
    Finding,
    Severity,
    VendorRecord,
)
from src.utils.file_utils import load_json, save_json


class VendorResolutionAgent(BaseAgent):
    name = "agent_c_vendor"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.log("Starting vendor resolution")
        invoice = context.get("extracted_invoice")
        packet = context.get("context_packet")

        if not invoice:
            self.log("No extracted invoice – skipping vendor resolution")
            context["vendor_resolved"] = None
            return context

        # Load vendor master data
        vendors = self._load_vendor_master(packet)
        self.log(f"Loaded {len(vendors)} vendor records")

        if not vendors:
            if invoice.vendor_name:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.NEW_VENDOR,
                    severity=Severity.WARNING,
                    confidence=1.0,
                    title="No vendor master data available",
                    description="Cannot resolve vendor – no vendor master data in bundle",
                    recommendation="Provide vendor master data or manually verify vendor",
                ))
            context["vendor_resolved"] = None
            context["vendor_master"] = []
            return context

        # Try to resolve vendor
        resolved, match_score = self._resolve_vendor(invoice, vendors)

        if resolved:
            self.log(f"Vendor resolved: {resolved.vendor_name} (score: {match_score})")
            context["vendor_resolved"] = resolved

            # Check for bank account changes
            self._check_bank_account(invoice, resolved)

            # Check vendor ID mismatch
            if invoice.vendor_id and invoice.vendor_id != resolved.vendor_id:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.VENDOR_MISMATCH,
                    severity=Severity.WARNING,
                    confidence=0.9,
                    title="Vendor ID mismatch",
                    description=(
                        f"Invoice vendor ID '{invoice.vendor_id}' does not match "
                        f"resolved vendor ID '{resolved.vendor_id}'"
                    ),
                    evidence=[EvidencePointer(
                        source_file="vendor_master",
                        field="vendor_id",
                        text_snippet=f"Expected: {resolved.vendor_id}",
                    )],
                    recommendation="Verify vendor identity before processing",
                ))

            # Check tax ID
            if invoice.vendor_tax_id and resolved.tax_id:
                if invoice.vendor_tax_id != resolved.tax_id:
                    self.add_finding(Finding(
                        agent=self.name,
                        category=ExceptionCategory.VENDOR_MISMATCH,
                        severity=Severity.WARNING,
                        confidence=0.9,
                        title="Vendor tax ID mismatch",
                        description=(
                            f"Invoice tax ID '{invoice.vendor_tax_id}' differs from "
                            f"master data '{resolved.tax_id}'"
                        ),
                        recommendation="Verify tax ID before processing",
                    ))
        else:
            self.log("Vendor could not be resolved")
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.NEW_VENDOR,
                severity=Severity.ERROR,
                confidence=1.0,
                title="Vendor not found in master data",
                description=(
                    f"Vendor '{invoice.vendor_name}' could not be matched to any "
                    f"vendor in master data (best score: {match_score})"
                ),
                recommendation="Create new vendor record or verify vendor name",
            ))
            context["vendor_resolved"] = None

        context["vendor_master"] = vendors
        save_json(
            {"resolved": resolved.model_dump() if resolved else None,
             "match_score": match_score,
             "findings_count": len(self.findings)},
            self.run_dir / "vendor_resolution.json",
        )
        return context

    def _load_vendor_master(self, packet) -> list[VendorRecord]:
        """Load vendor records from vendor_master document in bundle."""
        vendors = []
        for doc in packet.documents:
            if doc.document_type == DocumentType.VENDOR_MASTER:
                fpath = Path(doc.file_path)
                if fpath.exists() and fpath.suffix.lower() == ".json":
                    data = load_json(fpath)
                    if isinstance(data, list):
                        for v in data:
                            vendors.append(VendorRecord(**v))
                    elif isinstance(data, dict):
                        if "vendors" in data:
                            for v in data["vendors"]:
                                vendors.append(VendorRecord(**v))
                        else:
                            vendors.append(VendorRecord(**data))
        return vendors

    def _resolve_vendor(self, invoice, vendors: list[VendorRecord]) -> tuple[VendorRecord | None, float]:
        """Match invoice vendor to master data using fuzzy matching."""
        from rapidfuzz import fuzz

        threshold = self.policy.vendor_fuzzy_threshold
        best_match = None
        best_score = 0.0

        # Try vendor ID first (exact match)
        if invoice.vendor_id:
            for v in vendors:
                if v.vendor_id == invoice.vendor_id:
                    return v, 100.0

        # Fuzzy match on vendor name
        if invoice.vendor_name:
            for v in vendors:
                score = fuzz.ratio(
                    invoice.vendor_name.lower().strip(),
                    v.vendor_name.lower().strip(),
                )
                if score > best_score:
                    best_score = score
                    best_match = v

        if best_score >= threshold:
            return best_match, best_score
        return None, best_score

    def _check_bank_account(self, invoice, vendor: VendorRecord) -> None:
        """Check if bank account on invoice differs from master data."""
        if not invoice.vendor_bank_account or not vendor.bank_account:
            return

        if invoice.vendor_bank_account != vendor.bank_account:
            severity = Severity.CRITICAL
            description = (
                f"Bank account on invoice ({invoice.vendor_bank_account}) "
                f"differs from vendor master ({vendor.bank_account})"
            )

            # Check if change is recent
            if vendor.bank_account_last_changed:
                try:
                    change_date = datetime.fromisoformat(vendor.bank_account_last_changed)
                    threshold = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                        days=self.policy.anomaly_bank_change_days
                    )
                    if change_date > threshold:
                        description += (
                            f" – bank account was recently changed on "
                            f"{vendor.bank_account_last_changed}"
                        )
                except ValueError:
                    pass

            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.BANK_CHANGE,
                severity=severity,
                confidence=1.0,
                title="Bank account mismatch – possible fraud risk",
                description=description,
                evidence=[EvidencePointer(
                    source_file="vendor_master",
                    field="bank_account",
                    text_snippet=f"Master: {vendor.bank_account}",
                )],
                recommendation="Verify bank details directly with vendor before payment",
            ))