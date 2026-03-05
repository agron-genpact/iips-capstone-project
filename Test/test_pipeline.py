

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pipeline import Pipeline
from src.schemas.models import DecisionAction, MatchStatus, MatchType

BUNDLES_DIR = Path(__file__).parent / "bundles"
RUNS_DIR = Path(__file__).parent.parent / "test_runs"


def run_bundle(bundle_name: str, **kwargs) -> dict:
    
    bundle_path = BUNDLES_DIR / bundle_name
    pipeline = Pipeline(
        bundle_path=str(bundle_path),
        output_dir=str(RUNS_DIR),
        **kwargs,
    )
    return pipeline.run()


class TestCleanInvoice:
    """Scenario 1: Clean invoice with perfect PO + GRN match → auto-post."""

    def test_auto_post_decision(self):
        ctx = run_bundle("clean_invoice")
        decision = ctx["final_decision"]
        assert decision.decision == DecisionAction.AUTO_POST

    def test_match_result(self):
        ctx = run_bundle("clean_invoice")
        match = ctx["match_result"]
        assert match.match_type == MatchType.THREE_WAY
        assert match.overall_status == MatchStatus.MATCHED
        assert match.within_tolerance is True

    def test_artifacts_created(self):
        ctx = run_bundle("clean_invoice")
        run_id = ctx["run_id"]
        run_dir = RUNS_DIR / "runs" / run_id
        expected_files = [
            "context_packet.json",
            "extracted_invoice.json",
            "line_items.csv",
            "match_result.json",
            "final_decision.json",
            "posting_payload.json",
            "audit_log.md",
            "metrics.json",
        ]
        for fname in expected_files:
            assert (run_dir / fname).exists(), f"Missing artifact: {fname}"

    def test_posting_payload_generated(self):
        ctx = run_bundle("clean_invoice")
        decision = ctx["final_decision"]
        assert decision.posting_payload is not None
        assert decision.posting_payload.invoice_number == "INV-001"


class TestNoGRN:
    

    def test_route_for_approval(self):
        ctx = run_bundle("no_grn")
        decision = ctx["final_decision"]
        assert decision.decision in (
            DecisionAction.ROUTE_FOR_APPROVAL,
            DecisionAction.APPROVE_AND_POST,
        )

    def test_missing_grn_finding(self):
        ctx = run_bundle("no_grn")
        findings = ctx["all_findings"]
        grn_findings = [f for f in findings if "grn" in f.title.lower() or "grn" in f.category.value.lower()]
        assert len(grn_findings) > 0, "Expected a finding about missing GRN"


class TestQuantityVariance:
    """Scenario 3: Quantity variance beyond tolerance."""

    def test_variance_detected(self):
        ctx = run_bundle("quantity_variance")
        findings = ctx["all_findings"]
        qty_findings = [f for f in findings if f.category.value == "quantity_variance"]
        assert len(qty_findings) > 0, "Expected quantity variance findings"

    def test_not_auto_posted(self):
        ctx = run_bundle("quantity_variance")
        decision = ctx["final_decision"]
        assert decision.decision != DecisionAction.AUTO_POST


class TestPriceVariance:
    """Scenario 4: Price variance beyond tolerance."""

    def test_variance_detected(self):
        ctx = run_bundle("price_variance")
        findings = ctx["all_findings"]
        price_findings = [f for f in findings if f.category.value == "price_variance"]
        assert len(price_findings) > 0, "Expected price variance findings"


class TestHeaderMismatch:
    

    def test_mismatch_detected(self):
        ctx = run_bundle("header_mismatch")
        findings = ctx["all_findings"]
        total_findings = [f for f in findings if f.category.value == "total_mismatch"]
        assert len(total_findings) > 0, "Expected total mismatch finding"


class TestDuplicateInvoice:
  

    def test_processes_without_crash(self):
        ctx = run_bundle("duplicate_invoice")
        assert ctx["final_decision"] is not None


class TestCreditNote:
   

    def test_processes_credit_note(self):
        ctx = run_bundle("credit_note")
        invoice = ctx["extracted_invoice"]
        assert invoice is not None
        assert invoice.total_amount < 0


class TestMultiCurrency:
   

    def test_currency_flagged(self):
        ctx = run_bundle("multi_currency")
        findings = ctx["all_findings"]
        currency_findings = [
            f for f in findings
            if "currency" in f.title.lower() or "currency" in f.description.lower()
        ]
        assert len(currency_findings) > 0, "Expected currency compliance finding"


class TestTaxMismatch:
  

    def test_tax_mismatch_detected(self):
        ctx = run_bundle("tax_mismatch")
        findings = ctx["all_findings"]
        tax_findings = [f for f in findings if f.category.value == "tax_mismatch"]
        assert len(tax_findings) > 0, "Expected tax mismatch findings"


class TestNewVendor:
    """Scenario 10: Vendor not in master data."""

    def test_new_vendor_flagged(self):
        ctx = run_bundle("new_vendor")
        findings = ctx["all_findings"]
        vendor_findings = [f for f in findings if f.category.value == "new_vendor"]
        assert len(vendor_findings) > 0, "Expected new vendor finding"


class TestBankChangeAnomaly:
    
    def test_bank_change_detected(self):
        ctx = run_bundle("bank_change_anomaly")
        findings = ctx["all_findings"]
        bank_findings = [f for f in findings if f.category.value == "bank_change"]
        assert len(bank_findings) > 0, "Expected bank change finding"

    def test_held_or_reviewed(self):
        ctx = run_bundle("bank_change_anomaly")
        decision = ctx["final_decision"]
        assert decision.decision in (
            DecisionAction.HOLD,
            DecisionAction.ROUTE_FOR_APPROVAL,
            DecisionAction.REJECT,
        )


class TestLowOCRConfidence:
    

    def test_low_confidence_flagged(self):
        ctx = run_bundle("low_ocr_confidence")
        findings = ctx["all_findings"]
        conf_findings = [f for f in findings if f.category.value == "low_confidence"]
        assert len(conf_findings) > 0, "Expected low confidence findings"


class TestNoPOInvoice:
    """Scenario 13: Invoice with no PO."""

    def test_missing_po_flagged(self):
        ctx = run_bundle("no_po_invoice")
        findings = ctx["all_findings"]
        po_findings = [f for f in findings if f.category.value == "missing_po"]
        assert len(po_findings) > 0, "Expected missing PO finding"


class TestSplitDeliveries:
    
    def test_processes_split_delivery(self):
        ctx = run_bundle("split_deliveries")
        match = ctx["match_result"]
        assert match is not None
        assert match.match_type == MatchType.THREE_WAY

    def test_grn_quantities_aggregated(self):
        ctx = run_bundle("split_deliveries")
        match = ctx["match_result"]
       
        assert len(match.grn_numbers) == 2


class TestCleanSmallInvoice:
    

    def test_auto_post(self):
        ctx = run_bundle("clean_small_invoice")
        decision = ctx["final_decision"]
        assert decision.decision == DecisionAction.AUTO_POST

    def test_low_risk(self):
        ctx = run_bundle("clean_small_invoice")
        decision = ctx["final_decision"]
        assert decision.risk_score < 3.0
