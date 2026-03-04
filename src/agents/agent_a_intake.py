from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from src.agents.base import BaseAgent
from src.schemas.models import (
    ContextPacket,
    DocumentEntry,
    DocumentType,
    EvidencePointer,
)
from src.utils.file_utils import list_files, load_json, save_json


# Map file names / keywords to document types
_TYPE_KEYWORDS: dict[str, DocumentType] = {
    "invoice": DocumentType.INVOICE,
    "inv": DocumentType.INVOICE,
    "po": DocumentType.PURCHASE_ORDER,
    "purchase_order": DocumentType.PURCHASE_ORDER,
    "purchase-order": DocumentType.PURCHASE_ORDER,
    "grn": DocumentType.GRN,
    "goods_receipt": DocumentType.GRN,
    "receipt": DocumentType.GRN,
    "credit": DocumentType.CREDIT_NOTE,
    "credit_note": DocumentType.CREDIT_NOTE,
    "vendor_master": DocumentType.VENDOR_MASTER,
    "vendor-master": DocumentType.VENDOR_MASTER,
    "vendors": DocumentType.VENDOR_MASTER,
    "tax_rules": DocumentType.TAX_RULES,
    "tax-rules": DocumentType.TAX_RULES,
    "tax": DocumentType.TAX_RULES,
    "approval_policy": DocumentType.APPROVAL_POLICY,
    "approval-policy": DocumentType.APPROVAL_POLICY,
    "policy": DocumentType.APPROVAL_POLICY,
}

_SUPPORTED_EXTENSIONS = {"json", "yaml", "yml", "pdf", "png", "jpg", "jpeg", "tiff", "csv"}


class IntakeAgent(BaseAgent):
    name = "agent_a_intake"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        bundle_path = Path(context["bundle_path"])
        self.log(f"Starting intake from bundle: {bundle_path}")

        # Load manifest if present
        manifest = self._load_manifest(bundle_path)

        # Discover and classify documents
        documents = self._discover_documents(bundle_path, manifest)
        self.log(f"Discovered {len(documents)} documents")

        # Extract key references
        vendor_candidates = []
        po_refs = []
        grn_refs = []
        risk_indicators = []
        evidence_index = []

        for doc in documents:
            self._extract_references(doc, vendor_candidates, po_refs, grn_refs,
                                     risk_indicators, evidence_index, bundle_path)

        # Build context packet – use existing run_id from context if available
        run_id = context.get("run_id")
        packet_kwargs = {}
        if run_id:
            packet_kwargs["run_id"] = run_id
        packet = ContextPacket(
            **packet_kwargs,
            bundle_path=str(bundle_path),
            documents=documents,
            vendor_candidates=list(set(vendor_candidates)),
            po_references=list(set(po_refs)),
            grn_references=list(set(grn_refs)),
            risk_indicators=risk_indicators,
            evidence_index=evidence_index,
            metadata=manifest.get("metadata", {}),
        )

        # Risk: no PO references
        if not po_refs and self.policy.po_required:
            risk_indicators.append("no_po_reference_found")
            self.log("RISK: No PO references found in bundle")

        # Risk: no GRN
        if not grn_refs and self.policy.require_grn_for_goods:
            risk_indicators.append("no_grn_found")
            self.log("RISK: No GRN found in bundle")

        # Save artifacts
        save_json(packet, self.run_dir / "context_packet.json")
        self.log("Context packet saved")

        context["context_packet"] = packet
        context["run_id"] = packet.run_id
        return context

    def _load_manifest(self, bundle_path: Path) -> dict:
        """Load manifest.yaml if it exists in the bundle."""
        manifest_path = bundle_path / "manifest.yaml"
        if manifest_path.exists():
            with open(manifest_path) as f:
                self.log("Loaded manifest.yaml")
                return yaml.safe_load(f) or {}
        manifest_path = bundle_path / "manifest.yml"
        if manifest_path.exists():
            with open(manifest_path) as f:
                self.log("Loaded manifest.yml")
                return yaml.safe_load(f) or {}
        self.log("No manifest found – will auto-classify documents")
        return {}

    def _discover_documents(self, bundle_path: Path, manifest: dict) -> list[DocumentEntry]:
        """Find and classify all documents in the bundle."""
        documents = []

        # If manifest defines files, use that
        if "files" in manifest:
            for entry in manifest["files"]:
                fpath = bundle_path / entry["file"]
                doc_type = DocumentType(entry.get("type", "unknown"))
                documents.append(DocumentEntry(
                    file_path=str(fpath),
                    document_type=doc_type,
                    metadata=entry.get("metadata", {}),
                ))
            return documents

        # Auto-discover
        all_files = list_files(bundle_path, list(_SUPPORTED_EXTENSIONS))
        for fpath in all_files:
            if fpath.name.startswith(".") or fpath.name == "manifest.yaml":
                continue
            doc_type = self._classify_file(fpath)
            documents.append(DocumentEntry(
                file_path=str(fpath),
                document_type=doc_type,
            ))
        return documents

    def _classify_file(self, fpath: Path) -> DocumentType:
        """Classify a file by its name."""
        name_lower = fpath.stem.lower()
        for keyword, dtype in _TYPE_KEYWORDS.items():
            if keyword in name_lower:
                return dtype
        return DocumentType.UNKNOWN

    def _extract_references(
        self,
        doc: DocumentEntry,
        vendor_candidates: list[str],
        po_refs: list[str],
        grn_refs: list[str],
        risk_indicators: list[str],
        evidence_index: list[EvidencePointer],
        bundle_path: Path,
    ) -> None:
        """Scan a JSON document for key references."""
        fpath = Path(doc.file_path)
        if not fpath.exists() or fpath.suffix.lower() not in (".json", ".yaml", ".yml"):
            return

        try:
            if fpath.suffix.lower() == ".json":
                data = load_json(fpath)
            else:
                with open(fpath) as f:
                    data = yaml.safe_load(f) or {}
        except Exception:
            return

        if not isinstance(data, dict):
            return

        # PO references
        for key in ("po_number", "po_ref", "purchase_order_number"):
            if key in data and data[key]:
                po_refs.append(str(data[key]))
                evidence_index.append(EvidencePointer(
                    source_file=str(fpath), field=key, text_snippet=str(data[key])
                ))

        # GRN references
        for key in ("grn_number", "receipt_number"):
            if key in data and data[key]:
                grn_refs.append(str(data[key]))

        # Vendor candidates
        for key in ("vendor_name", "supplier_name"):
            if key in data and data[key]:
                vendor_candidates.append(str(data[key]))

        # Vendor ID
        if doc.document_type == DocumentType.INVOICE:
            if "vendor_id" not in data or not data.get("vendor_id"):
                risk_indicators.append(f"invoice_missing_vendor_id:{fpath.name}")