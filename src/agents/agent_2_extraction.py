from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    DocumentType,
    EvidencePointer,
    ExceptionCategory,
    ExtractedInvoice,
    Finding,
    LineItem,
    Severity,
)
from src.utils.file_utils import load_json, save_csv, save_json


class ExtractionAgent(BaseAgent):
    name = "agent_b_extraction"

    @staticmethod
    def _find_bbox_for_text(
        word_boxes: list[dict], search_text: str
    ) -> list[float] | None:
        if not word_boxes or not search_text:
            return None

        search_lower = search_text.lower().strip()
        for wb in word_boxes:
            if wb["text"].lower().strip() == search_lower:
                return wb["bbox"]

        search_tokens = search_lower.split()
        if len(search_tokens) < 2:
            # partial match fallback
            for wb in word_boxes:
                if search_lower in wb["text"].lower():
                    return wb["bbox"]
            return None

        for i in range(len(word_boxes) - len(search_tokens) + 1):
            window = word_boxes[i : i + len(search_tokens)]
            window_text = " ".join(w["text"].lower().strip() for w in window)
            if search_lower in window_text:
                x0 = min(w["bbox"][0] for w in window)
                y0 = min(w["bbox"][1] for w in window)
                x1 = max(w["bbox"][2] for w in window)
                y1 = max(w["bbox"][3] for w in window)
                return [x0, y0, x1, y1]

        return None

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        packet = context["context_packet"]
        self.log("Starting extraction")

        invoice_docs = [
            d for d in packet.documents
            if d.document_type == DocumentType.INVOICE
        ]

        if not invoice_docs:
            self.log("WARNING: No invoice documents found in bundle")
            context["extracted_invoice"] = None
            return context

        doc = invoice_docs[0]
        fpath = Path(doc.file_path)
        self.log(f"Extracting from: {fpath.name}")

        if fpath.suffix.lower() == ".json":
            invoice = self._extract_from_json(fpath)
        elif fpath.suffix.lower() in (".pdf",):
            invoice = self._extract_from_pdf(fpath)
        elif fpath.suffix.lower() in (".png", ".jpg", ".jpeg", ".tiff"):
            invoice = self._extract_from_image(fpath)
        else:
            self.log(f"Unsupported format: {fpath.suffix}")
            invoice = ExtractedInvoice()

        self._check_extraction_quality(invoice, fpath)

        save_json(invoice, self.run_dir / "extracted_invoice.json")

        if invoice.line_items:
            rows = [item.model_dump() for item in invoice.line_items]
            save_csv(rows, self.run_dir / "line_items.csv")
            self.log(f"Extracted {len(invoice.line_items)} line items")

        context["extracted_invoice"] = invoice
        return context

    def _extract_from_json(self, fpath: Path) -> ExtractedInvoice:
        data = load_json(fpath)
        self.log("Parsing structured JSON invoice")

        line_items = []
        for i, item in enumerate(data.get("line_items", []), 1):
            line_items.append(LineItem(
                line_number=item.get("line_number", i),
                description=item.get("description") or "",
                quantity=float(item.get("quantity") or 0),
                unit=item.get("unit"),
                unit_price=float(item.get("unit_price") or 0),
                amount=float(item.get("amount") or 0),
                tax_rate=item.get("tax_rate"),
                tax_amount=item.get("tax_amount"),
                po_line_ref=item.get("po_line_ref"),
                confidence=float(item.get("confidence") or 0.5),
            ))

        confidence_scores = {}
        for field in ["invoice_number", "invoice_date", "vendor_name",
                      "total_amount", "po_number", "line_items"]:
            if data.get(field):
                confidence_scores[field] = 1.0
            else:
                confidence_scores[field] = 0.0

        invoice = ExtractedInvoice(
            invoice_number=data.get("invoice_number"),
            invoice_date=data.get("invoice_date"),
            due_date=data.get("due_date"),
            vendor_name=data.get("vendor_name"),
            vendor_id=data.get("vendor_id"),
            vendor_address=data.get("vendor_address"),
            vendor_tax_id=data.get("vendor_tax_id"),
            vendor_bank_account=data.get("vendor_bank_account"),
            buyer_name=data.get("buyer_name"),
            buyer_address=data.get("buyer_address"),
            buyer_tax_id=data.get("buyer_tax_id"),
            po_number=data.get("po_number"),
            currency=data.get("currency") or "USD",
            subtotal=data.get("subtotal"),
            tax_amount=data.get("tax_amount"),
            total_amount=data.get("total_amount"),
            line_items=line_items,
            payment_terms=data.get("payment_terms"),
            notes=data.get("notes"),
            confidence_scores=confidence_scores,
            evidence=[EvidencePointer(source_file=str(fpath), field="full_document")],
        )
        return invoice

    def _extract_from_pdf(self, fpath: Path) -> ExtractedInvoice:
        try:
            import pdfplumber
        except ImportError:
            self.log("pdfplumber not available – falling back to empty extraction")
            return ExtractedInvoice(
                evidence=[EvidencePointer(source_file=str(fpath), field="pdf_extraction_failed")],
                confidence_scores={"overall": 0.0},
            )

        invoice = ExtractedInvoice()
        all_text = ""
        word_boxes: list[dict] = []

        with pdfplumber.open(fpath) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                all_text += text + "\n"

                for w in page.extract_words() or []:
                    word_boxes.append({
                        "text": w.get("text", ""),
                        "bbox": [
                            round(w.get("x0", 0), 2),
                            round(w.get("top", 0), 2),
                            round(w.get("x1", 0), 2),
                            round(w.get("bottom", 0), 2),
                        ],
                        "page": page_num,
                    })

                tables = page.extract_tables()
                table_bboxes = [
                    t.bbox for t in (page.find_tables() or [])
                ]
                for idx, table in enumerate(tables):
                    if table and len(table) > 1:
                        t_bbox = table_bboxes[idx] if idx < len(table_bboxes) else None
                        invoice.line_items.extend(
                            self._parse_table_to_line_items(
                                table, fpath, page_num, table_bbox=t_bbox,
                            )
                        )

        invoice = self._parse_text_fields(invoice, all_text, fpath, word_boxes=word_boxes)
        self.log(f"Collected {len(word_boxes)} word bounding boxes from PDF")
        return invoice

    def _extract_from_image(self, fpath: Path) -> ExtractedInvoice:
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            self.log("pytesseract/PIL not available – falling back to empty extraction")
            return ExtractedInvoice(
                evidence=[EvidencePointer(source_file=str(fpath), field="ocr_failed")],
                confidence_scores={"overall": 0.0},
            )

        img = Image.open(fpath)

        try:
            ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            word_boxes: list[dict] = []
            text_parts: list[str] = []

            for i, word_text in enumerate(ocr_data.get("text", [])):
                word_text = str(word_text).strip()
                if not word_text:
                    if text_parts and text_parts[-1] != "\n":
                        text_parts.append("\n")
                    continue
                text_parts.append(word_text)
                left = ocr_data["left"][i]
                top = ocr_data["top"][i]
                width = ocr_data["width"][i]
                height = ocr_data["height"][i]
                word_boxes.append({
                    "text": word_text,
                    "bbox": [
                        round(float(left), 2),
                        round(float(top), 2),
                        round(float(left + width), 2),
                        round(float(top + height), 2),
                    ],
                    "page": 1,
                })

            text = " ".join(t for t in text_parts if t != "\n")
            full_text_lines: list[str] = []
            current_line: list[str] = []
            for t in text_parts:
                if t == "\n":
                    if current_line:
                        full_text_lines.append(" ".join(current_line))
                        current_line = []
                else:
                    current_line.append(t)
            if current_line:
                full_text_lines.append(" ".join(current_line))
            text = "\n".join(full_text_lines)

            self.log(f"Collected {len(word_boxes)} word bounding boxes from OCR")
        except Exception:
            text = pytesseract.image_to_string(img)
            word_boxes = []

        invoice = ExtractedInvoice()
        invoice = self._parse_text_fields(invoice, text, fpath, word_boxes=word_boxes or None)

        for key in invoice.confidence_scores:
            invoice.confidence_scores[key] *= 0.7

        return invoice

    def _parse_text_fields(
        self,
        invoice: ExtractedInvoice,
        text: str,
        fpath: Path,
        word_boxes: list[dict] | None = None,
    ) -> ExtractedInvoice:
        confidence = {}

        # Invoice number
        m = re.search(r"(?:invoice\s*(?:#|no\.?|number)\s*[:.]?\s*)([A-Za-z0-9\-]+)", text, re.I)
        if m:
            invoice.invoice_number = m.group(1).strip()
            confidence["invoice_number"] = 0.8
            bbox = self._find_bbox_for_text(word_boxes or [], m.group(1).strip())
            invoice.evidence.append(EvidencePointer(
                source_file=str(fpath), field="invoice_number",
                text_snippet=invoice.invoice_number, bbox=bbox,
            ))

        # Invoice date
        m = re.search(r"(?:invoice\s*date|date)\s*[:.]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", text, re.I)
        if m:
            invoice.invoice_date = m.group(1).strip()
            confidence["invoice_date"] = 0.8
            bbox = self._find_bbox_for_text(word_boxes or [], m.group(1).strip())
            invoice.evidence.append(EvidencePointer(
                source_file=str(fpath), field="invoice_date",
                text_snippet=invoice.invoice_date, bbox=bbox,
            ))

        # PO Number
        m = re.search(r"(?:PO|purchase\s*order)\s*(?:#|no\.?|number)?\s*[:.]?\s*([A-Za-z0-9\-]+)", text, re.I)
        if m:
            invoice.po_number = m.group(1).strip()
            confidence["po_number"] = 0.8
            bbox = self._find_bbox_for_text(word_boxes or [], m.group(1).strip())
            invoice.evidence.append(EvidencePointer(
                source_file=str(fpath), field="po_number",
                text_snippet=invoice.po_number, bbox=bbox,
            ))

        # Total amount
        m = re.search(r"(?:total|amount\s*due|balance\s*due)\s*[:.]?\s*\$?\s*([\d,]+\.?\d*)", text, re.I)
        if m:
            invoice.total_amount = float(m.group(1).replace(",", ""))
            confidence["total_amount"] = 0.75
            bbox = self._find_bbox_for_text(word_boxes or [], m.group(1).strip())
            invoice.evidence.append(EvidencePointer(
                source_file=str(fpath), field="total_amount",
                text_snippet=str(invoice.total_amount), bbox=bbox,
            ))

        # Vendor name
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if lines:
            invoice.vendor_name = lines[0]
            confidence["vendor_name"] = 0.5
            bbox = self._find_bbox_for_text(word_boxes or [], lines[0])
            invoice.evidence.append(EvidencePointer(
                source_file=str(fpath), field="vendor_name",
                text_snippet=invoice.vendor_name, bbox=bbox,
            ))

        invoice.confidence_scores = confidence
        invoice.evidence.append(EvidencePointer(source_file=str(fpath), field="text_extraction"))
        return invoice

    def _parse_table_to_line_items(
        self,
        table: list[list],
        fpath: Path,
        page: int,
        table_bbox: tuple | None = None,
    ) -> list[LineItem]:
        """Try to parse a table into line items."""
        items = []
        headers = [str(h).lower().strip() if h else "" for h in table[0]]

        desc_col = next((i for i, h in enumerate(headers) if "desc" in h or "item" in h), None)
        qty_col = next((i for i, h in enumerate(headers) if "qty" in h or "quant" in h), None)
        price_col = next((i for i, h in enumerate(headers) if "price" in h or "rate" in h or "unit" in h), None)
        amount_col = next((i for i, h in enumerate(headers) if "amount" in h or "total" in h or "ext" in h), None)

        for row_idx, row in enumerate(table[1:], 1):
            try:
                desc = str(row[desc_col]) if desc_col is not None and desc_col < len(row) else ""
                qty = self._safe_float(row[qty_col] if qty_col is not None and qty_col < len(row) else "0")
                price = self._safe_float(row[price_col] if price_col is not None and price_col < len(row) else "0")
                amount = self._safe_float(row[amount_col] if amount_col is not None and amount_col < len(row) else "0")

                if desc or amount > 0:
                    items.append(LineItem(
                        line_number=row_idx,
                        description=desc,
                        quantity=qty,
                        unit_price=price,
                        amount=amount,
                        confidence=0.7,
                    ))
            except (ValueError, IndexError):
                continue

        return items

    def _safe_float(self, val: Any) -> float:
        if val is None:
            return 0.0
        try:
            return float(str(val).replace(",", "").replace("$", "").strip())
        except (ValueError, TypeError):
            return 0.0

    def _check_extraction_quality(self, invoice: ExtractedInvoice, fpath: Path) -> None:
        min_conf = self.policy.min_ocr_confidence

        required_fields = {
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date,
            "vendor_name": invoice.vendor_name,
            "total_amount": invoice.total_amount,
        }

        for field_name, value in required_fields.items():
            conf = invoice.confidence_scores.get(field_name, 0.0 if value is None else 1.0)
            if value is None or conf < min_conf:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.LOW_CONFIDENCE,
                    severity=Severity.WARNING if value is not None else Severity.ERROR,
                    confidence=conf,
                    title=f"Low confidence: {field_name}",
                    description=f"Field '{field_name}' has confidence {conf:.2f} (threshold: {min_conf})",
                    evidence=[EvidencePointer(source_file=str(fpath), field=field_name)],
                    recommendation="Manual review recommended" if conf < min_conf else None,
                ))