from __future__ import annotations

import json
import sys
import types
from copy import deepcopy
from pathlib import Path

import yaml
from click.testing import CliRunner

from src.agents.agent_b_extraction import ExtractionAgent
from src.cli import main as cli_main
from src.pipeline import Pipeline
from src.schemas.models import DecisionAction, ExtractedInvoice, MatchStatus, MatchType
from src.utils.file_utils import mask_sensitive_data
from src.utils.policy import Policy

BUNDLES_DIR = Path(__file__).parent.parent / "data_inputs" / "bundles"
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
    def test_route_for_receipt_confirmation(self):
        ctx = run_bundle("no_grn")
        decision = ctx["final_decision"]
        assert decision.decision == DecisionAction.ROUTE_FOR_APPROVAL

    def test_missing_grn_finding(self):
        ctx = run_bundle("no_grn")
        findings = ctx["all_findings"]
        grn_findings = [
            f for f in findings if "grn" in f.title.lower() or "grn" in f.category.value.lower()
        ]
        assert len(grn_findings) > 0, "Expected a finding about missing GRN"


class TestQuantityVariance:
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

    def test_exact_duplicate_flagged(self):
        ctx = run_bundle("duplicate_invoice")
        findings = [f for f in ctx["all_findings"] if f.category.value == "duplicate"]
        assert findings, "Expected duplicate finding"
        assert any(f.severity.value == "critical" for f in findings)


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
            f for f in findings if "currency" in f.title.lower() or "currency" in f.description.lower()
        ]
        assert len(currency_findings) > 0, "Expected currency compliance finding"


class TestTaxMismatch:
    def test_tax_mismatch_detected(self):
        ctx = run_bundle("tax_mismatch")
        findings = ctx["all_findings"]
        tax_findings = [f for f in findings if f.category.value == "tax_mismatch"]
        assert len(tax_findings) > 0, "Expected tax mismatch findings"


class TestNewVendor:
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
    def test_missing_po_flagged(self):
        ctx = run_bundle("no_po_invoice")
        findings = ctx["all_findings"]
        po_findings = [f for f in findings if f.category.value == "missing_po"]
        assert len(po_findings) > 0, "Expected missing PO finding"

    def test_non_po_policy_routed(self):
        ctx = run_bundle("no_po_invoice")
        assert ctx["final_decision"].decision == DecisionAction.ROUTE_FOR_APPROVAL


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


class TestPostingPayloadAndDecisionConsistency:
    def test_posting_payload_exists_for_non_post_decision(self):
        ctx = run_bundle("no_grn")
        run_dir = RUNS_DIR / "runs" / ctx["run_id"]
        assert (run_dir / "posting_payload.json").exists()
        assert ctx["final_decision"].posting_payload is not None

    def test_posting_payload_decision_matches_final_decision(self):
        ctx = run_bundle("no_grn")
        final_decision = ctx["final_decision"].decision
        payload_decision = ctx["final_decision"].posting_payload.decision
        assert payload_decision == final_decision

    def test_approval_packet_recommended_action_matches_final_decision(self):
        ctx = run_bundle("no_grn")
        assert ctx["approval_packet"].recommended_action == ctx["final_decision"].decision


class TestSingleFileInput:
    def test_single_pdf_file_supported(self, tmp_path, monkeypatch):
        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(b"%PDF-1.4\n%dummy\n")

        def fake_pdf_extract(self, fpath):
            return ExtractedInvoice(
                invoice_number="PDF-INV-1",
                invoice_date="2025-01-01",
                vendor_name="PDF Vendor",
                total_amount=100.0,
                currency="USD",
                confidence_scores={
                    "invoice_number": 1.0,
                    "invoice_date": 1.0,
                    "vendor_name": 1.0,
                    "total_amount": 1.0,
                },
            )

        monkeypatch.setattr(ExtractionAgent, "_extract_from_pdf", fake_pdf_extract)
        ctx = Pipeline(bundle_path=str(pdf_file), output_dir=str(tmp_path)).run()

        assert len(ctx["context_packet"].documents) == 1
        assert ctx["context_packet"].documents[0].file_path == str(pdf_file)
        assert ctx["context_packet"].documents[0].document_type.value == "invoice"

    def test_single_image_file_supported(self, tmp_path, monkeypatch):
        image_file = tmp_path / "invoice.png"
        image_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        def fake_image_extract(self, fpath):
            return ExtractedInvoice(
                invoice_number="IMG-INV-1",
                invoice_date="2025-01-01",
                vendor_name="Image Vendor",
                total_amount=200.0,
                currency="USD",
                confidence_scores={
                    "invoice_number": 1.0,
                    "invoice_date": 1.0,
                    "vendor_name": 1.0,
                    "total_amount": 1.0,
                },
            )

        monkeypatch.setattr(ExtractionAgent, "_extract_from_image", fake_image_extract)
        ctx = Pipeline(bundle_path=str(image_file), output_dir=str(tmp_path)).run()
        assert ctx["context_packet"].documents[0].file_path == str(image_file)


class TestMissingTesseractBehavior:
    def test_missing_tesseract_does_not_crash_and_adds_low_confidence_finding(self, tmp_path, monkeypatch):
        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.Output = types.SimpleNamespace(DICT="DICT")
        fake_pil = types.ModuleType("PIL")
        fake_pil.Image = object()

        monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
        monkeypatch.setitem(sys.modules, "PIL", fake_pil)
        monkeypatch.setattr("src.agents.agent_b_extraction.shutil.which", lambda _: None)

        agent = ExtractionAgent(run_dir=tmp_path, policy=Policy())
        invoice = agent._extract_from_image(tmp_path / "invoice.png")

        assert invoice.confidence_scores.get("overall") == 0.0
        assert any("tesseract" in f.title.lower() for f in agent.findings)


class TestCLIList:
    def test_lists_runs_from_output_runs_subfolder(self, tmp_path):
        output_root = tmp_path / "out"
        run_dir = output_root / "runs" / "run-123"
        run_dir.mkdir(parents=True)
        (run_dir / "final_decision.json").write_text(json.dumps({"decision": "auto_post"}))

        runner = CliRunner()
        result = runner.invoke(cli_main, ["list", "--output", str(output_root)])

        assert result.exit_code == 0
        assert "run-123" in result.output
        assert "auto_post" in result.output


class TestDeterministicPolicy:
    def test_strict_mode_stabilizes_run_id(self, tmp_path):
        base_policy_path = Path(__file__).parent.parent / "config" / "policy.yaml"
        with open(base_policy_path, "r") as f:
            policy_data = yaml.safe_load(f)

        policy_data.setdefault("reproducibility", {})["strict_mode"] = True
        policy_path = tmp_path / "strict_policy.yaml"
        with open(policy_path, "w") as f:
            yaml.safe_dump(policy_data, f)

        bundle = BUNDLES_DIR / "clean_small_invoice"
        ctx1 = Pipeline(
            bundle_path=str(bundle),
            output_dir=str(tmp_path),
            policy_path=str(policy_path),
        ).run()
        ctx2 = Pipeline(
            bundle_path=str(bundle),
            output_dir=str(tmp_path),
            policy_path=str(policy_path),
        ).run()

        assert ctx1["run_id"] == ctx2["run_id"]

    def test_finding_ids_are_deterministic(self):
        ctx1 = run_bundle("no_grn")
        ctx2 = run_bundle("no_grn")
        ids1 = [f.finding_id for f in ctx1["all_findings"]]
        ids2 = [f.finding_id for f in ctx2["all_findings"]]
        assert ids1 == ids2


class TestArtifactsAndPrivacy:
    def test_line_items_csv_created_even_when_no_line_items(self):
        ctx = run_bundle("bank_change_anomaly")
        run_dir = RUNS_DIR / "runs" / ctx["run_id"]
        assert (run_dir / "line_items.csv").exists()

    def test_masking_does_not_corrupt_posting_payload_structure(self):
        payload = {
            "posting_payload": {"decision": "auto_post", "status": "ready"},
            "vendor_tax_id": "US12-3456789",
            "vendor_bank_account": "BANK-001-ACME",
        }
        masked = mask_sensitive_data(deepcopy(payload), {
            "mask_bank_details_in_logs": True,
            "mask_tax_ids_in_logs": True,
            "mask_sensitive_artifacts": True,
        })
        assert isinstance(masked["posting_payload"], dict)
        assert masked["posting_payload"]["decision"] == "auto_post"
        assert masked["vendor_tax_id"] != payload["vendor_tax_id"]
        assert masked["vendor_bank_account"] != payload["vendor_bank_account"]

    def test_final_decision_keeps_posting_payload_object(self):
        ctx = run_bundle("clean_small_invoice")
        run_dir = RUNS_DIR / "runs" / ctx["run_id"]
        with open(run_dir / "final_decision.json") as f:
            final = json.load(f)
        assert isinstance(final.get("posting_payload"), dict)
        assert final["posting_payload"].get("decision") == final.get("decision")


class TestEvidencePointers:
    def test_critical_and_error_findings_have_concrete_evidence(self):
        ctx = run_bundle("duplicate_invoice")
        concrete_only = {"vendor_master", "invoice_history.json"}
        for finding in ctx["final_decision"].all_findings:
            if finding.severity.value not in ("critical", "error"):
                continue
            assert finding.evidence, f"Missing evidence for {finding.title}"
            for ev in finding.evidence:
                assert ev.source_file
                assert not ev.source_file.endswith(":context")
                assert ev.source_file not in concrete_only


class TestBundleApprovalPolicyOverrides:
    def test_bundle_approval_policy_override_is_applied(self, tmp_path):
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        invoice = {
            "invoice_number": "INV-OVR-1",
            "invoice_date": "2025-01-01",
            "vendor_name": "Override Vendor",
            "vendor_id": "V-OVR",
            "currency": "USD",
            "total_amount": 100.0,
            "line_items": [
                {"line_number": 1, "description": "Service", "quantity": 1, "unit_price": 100, "amount": 100}
            ],
        }
        (bundle / "invoice.json").write_text(json.dumps(invoice))
        (bundle / "vendor_master.json").write_text(json.dumps([{"vendor_id": "V-OVR", "vendor_name": "Override Vendor"}]))
        (bundle / "approval_policy.yaml").write_text(
            "matching:\n"
            "  non_po_routing: reject\n"
            "  po_required: true\n"
        )

        ctx = Pipeline(bundle_path=str(bundle), output_dir=str(tmp_path / "out")).run()
        assert ctx["final_decision"].decision == DecisionAction.REJECT


class TestQualityBenchmarks:
    def test_extraction_accuracy_kpi(self):
        bundles = ["clean_invoice", "clean_small_invoice", "no_grn"]
        scores = []
        for name in bundles:
            ctx = run_bundle(name)
            run_dir = RUNS_DIR / "runs" / ctx["run_id"]
            with open(run_dir / "metrics.json") as f:
                metrics = json.load(f)
            scores.append(metrics.get("extraction_field_accuracy", 0.0))
        avg_score = sum(scores) / len(scores)
        assert avg_score >= 0.95

    def test_matching_precision_kpi(self):
        bundles = ["clean_invoice", "split_deliveries"]
        precisions = []
        for name in bundles:
            ctx = run_bundle(name)
            run_dir = RUNS_DIR / "runs" / ctx["run_id"]
            with open(run_dir / "metrics.json") as f:
                metrics = json.load(f)
            precisions.append(metrics.get("matching_line_precision", 0.0))
        avg_precision = sum(precisions) / len(precisions)
        assert avg_precision >= 0.95


class TestMultiPagePDFAggregation:
    def test_pdf_multi_page_text_and_bbox_aggregation(self, tmp_path, monkeypatch):
        class FakeTable:
            def __init__(self, bbox):
                self.bbox = bbox

        class FakePage:
            def __init__(self, text, words):
                self._text = text
                self._words = words

            def extract_text(self):
                return self._text

            def extract_words(self):
                return self._words

            def extract_tables(self):
                return []

            def find_tables(self):
                return [FakeTable((0, 0, 1, 1))]

        class FakePDF:
            def __init__(self, pages):
                self.pages = pages

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        captured: dict[str, object] = {}

        def fake_parse_text_fields(self, invoice, text, fpath, word_boxes=None):
            captured["text"] = text
            captured["word_boxes"] = word_boxes or []
            invoice.invoice_number = "INV-MP-001"
            invoice.invoice_date = "2025-01-01"
            invoice.vendor_name = "Multi Page Vendor"
            invoice.total_amount = 100.0
            invoice.confidence_scores = {
                "invoice_number": 1.0,
                "invoice_date": 1.0,
                "vendor_name": 1.0,
                "total_amount": 1.0,
            }
            return invoice

        def fake_open(_fpath):
            return FakePDF([
                FakePage(
                    "Invoice Number: INV-MP-001",
                    [{"text": "INV-MP-001", "x0": 1, "top": 2, "x1": 3, "bottom": 4}],
                ),
                FakePage(
                    "Total: 100.00",
                    [{"text": "100.00", "x0": 5, "top": 6, "x1": 7, "bottom": 8}],
                ),
            ])

        fake_pdfplumber = types.ModuleType("pdfplumber")
        fake_pdfplumber.open = fake_open
        monkeypatch.setitem(sys.modules, "pdfplumber", fake_pdfplumber)
        monkeypatch.setattr(ExtractionAgent, "_parse_text_fields", fake_parse_text_fields)

        agent = ExtractionAgent(run_dir=tmp_path, policy=Policy())
        _ = agent._extract_from_pdf(tmp_path / "multi.pdf")

        merged_text = str(captured.get("text", ""))
        assert "Invoice Number: INV-MP-001" in merged_text
        assert "Total: 100.00" in merged_text
        pages = {wb["page"] for wb in captured.get("word_boxes", [])}
        assert pages == {1, 2}
