from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.schemas.models import (
    DocumentType,
    EvidencePointer,
    ExceptionCategory,
    ExtractedInvoice,
    Finding,
    GoodsReceiptNote,
    GRNLineItem,
    LineMatchResult,
    MatchResult,
    MatchStatus,
    MatchType,
    POLineItem,
    PurchaseOrder,
    Severity,
)
from src.utils.file_utils import load_json, save_json


class MatchingAgent(BaseAgent):
    name = "agent_e_matching"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self.log("Starting matching engine")
        invoice: ExtractedInvoice | None = context.get("extracted_invoice")
        packet = context.get("context_packet")

        if not invoice:
            self.log("No extracted invoice – skipping matching")
            context["match_result"] = None
            return context

        # Load PO and GRN data
        pos = self._load_purchase_orders(packet)
        grns = self._load_grns(packet)

        self.log(f"Loaded {len(pos)} POs and {len(grns)} GRNs")

        # Determine match type
        if not pos:
            self.log("No PO data – no match possible")
            result = MatchResult(
                match_type=MatchType.NO_MATCH,
                overall_status=MatchStatus.UNMATCHED,
                invoice_number=invoice.invoice_number,
                summary="No PO data available for matching",
            )
            if self.policy.po_required:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.MISSING_PO,
                    severity=Severity.ERROR,
                    confidence=1.0,
                    title="No purchase order for matching",
                    description="Invoice has no associated PO and policy requires PO matching",
                    recommendation="Route for non-PO invoice approval",
                ))
            result.findings = list(self.findings)
            save_json(result, self.run_dir / "match_result.json")
            context["match_result"] = result
            return context

        # Find the matching PO
        po = self._find_matching_po(invoice, pos)
        if not po:
            self.log("No matching PO found")
            result = MatchResult(
                match_type=MatchType.NO_MATCH,
                overall_status=MatchStatus.UNMATCHED,
                invoice_number=invoice.invoice_number,
                summary="Could not find a matching PO",
            )
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.MISSING_PO,
                severity=Severity.ERROR,
                confidence=1.0,
                title="PO not found",
                description=f"No PO matches invoice PO ref '{invoice.po_number}'",
                recommendation="Verify PO number and resubmit",
            ))
            result.findings = list(self.findings)
            save_json(result, self.run_dir / "match_result.json")
            context["match_result"] = result
            return context

        # Find matching GRNs
        matching_grns = self._find_matching_grns(po, grns)

        if matching_grns and self.policy.require_grn_for_goods:
            match_type = MatchType.THREE_WAY
            self.log(f"Performing 3-way match with PO {po.po_number} and {len(matching_grns)} GRNs")
        else:
            match_type = MatchType.TWO_WAY
            self.log(f"Performing 2-way match with PO {po.po_number}")

            if not matching_grns and self.policy.require_grn_for_goods:
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.MISSING_GRN,
                    severity=Severity.WARNING,
                    confidence=1.0,
                    title="No GRN for 3-way matching",
                    description="GRN required for goods invoices but none found",
                    recommendation="Obtain goods receipt confirmation before payment",
                ))

        # Perform line-level matching
        line_matches = self._match_lines(invoice, po, matching_grns, match_type)

        # Calculate overall status
        overall_status = self._compute_overall_status(line_matches)

        # Total-level comparison – compare pre-tax subtotals when possible
        # Invoice total often includes tax, PO total is typically pre-tax
        invoice_comparable = invoice.subtotal if invoice.subtotal is not None else (
            sum(li.amount for li in invoice.line_items) if invoice.line_items else (invoice.total_amount or 0)
        )
        total_po = po.total_amount or sum(li.amount for li in po.line_items)
        total_invoice = invoice.total_amount or 0
        total_variance = round(invoice_comparable - total_po, 2)
        total_variance_pct = round((total_variance / total_po * 100), 2) if total_po else 0

        within_tolerance = (
            abs(total_variance_pct) <= self.policy.total_tolerance_pct
            and abs(total_variance) <= self.policy.absolute_max_tolerance
        )

        if not within_tolerance and total_variance != 0:
            severity = Severity.ERROR if abs(total_variance_pct) > self.policy.total_tolerance_pct * 2 else Severity.WARNING
            self.add_finding(Finding(
                agent=self.name,
                category=ExceptionCategory.PRICE_VARIANCE,
                severity=severity,
                confidence=1.0,
                title="Total amount variance exceeds tolerance",
                description=(
                    f"Invoice total ({total_invoice}) vs PO total ({total_po}): "
                    f"variance {total_variance} ({total_variance_pct}%)"
                ),
                data={"invoice_total": total_invoice, "po_total": total_po,
                      "variance": total_variance, "variance_pct": total_variance_pct},
                recommendation="Review pricing and approve variance",
            ))

        result = MatchResult(
            match_type=match_type,
            overall_status=overall_status,
            invoice_number=invoice.invoice_number,
            po_number=po.po_number,
            grn_numbers=[g.grn_number for g in matching_grns],
            line_matches=line_matches,
            total_invoice=total_invoice,
            total_po=total_po,
            total_variance=total_variance,
            total_variance_pct=total_variance_pct,
            within_tolerance=within_tolerance,
            findings=list(self.findings),
            summary=self._build_summary(overall_status, match_type, line_matches),
        )

        save_json(result, self.run_dir / "match_result.json")
        context["match_result"] = result
        self.log(f"Matching complete: {overall_status.value}")
        return context

    def _load_purchase_orders(self, packet) -> list[PurchaseOrder]:
        pos = []
        for doc in packet.documents:
            if doc.document_type == DocumentType.PURCHASE_ORDER:
                fpath = Path(doc.file_path)
                if fpath.exists() and fpath.suffix.lower() == ".json":
                    data = load_json(fpath)
                    if isinstance(data, dict):
                        line_items = [POLineItem(**li) for li in data.get("line_items", [])]
                        pos.append(PurchaseOrder(
                            po_number=data.get("po_number", ""),
                            vendor_name=data.get("vendor_name"),
                            vendor_id=data.get("vendor_id"),
                            order_date=data.get("order_date"),
                            currency=data.get("currency", "USD"),
                            total_amount=data.get("total_amount"),
                            line_items=line_items,
                            payment_terms=data.get("payment_terms"),
                        ))
        return pos

    def _load_grns(self, packet) -> list[GoodsReceiptNote]:
        grns = []
        for doc in packet.documents:
            if doc.document_type == DocumentType.GRN:
                fpath = Path(doc.file_path)
                if fpath.exists() and fpath.suffix.lower() == ".json":
                    data = load_json(fpath)
                    if isinstance(data, dict):
                        line_items = [GRNLineItem(**li) for li in data.get("line_items", [])]
                        grns.append(GoodsReceiptNote(
                            grn_number=data.get("grn_number", ""),
                            po_number=data.get("po_number"),
                            vendor_name=data.get("vendor_name"),
                            receipt_date=data.get("receipt_date"),
                            line_items=line_items,
                        ))
        return grns

    def _find_matching_po(self, invoice: ExtractedInvoice, pos: list[PurchaseOrder]) -> PurchaseOrder | None:
        if invoice.po_number:
            for po in pos:
                if po.po_number == invoice.po_number:
                    return po
        # If only one PO, use it
        if len(pos) == 1:
            return pos[0]
        return None

    def _find_matching_grns(self, po: PurchaseOrder, grns: list[GoodsReceiptNote]) -> list[GoodsReceiptNote]:
        return [g for g in grns if g.po_number == po.po_number]

    def _match_lines(
        self,
        invoice: ExtractedInvoice,
        po: PurchaseOrder,
        grns: list[GoodsReceiptNote],
        match_type: MatchType,
    ) -> list[LineMatchResult]:
        results = []
        # Aggregate GRN quantities by PO line
        grn_qty_map: dict[int, float] = {}
        for grn in grns:
            for gl in grn.line_items:
                ref = gl.po_line_ref or str(gl.line_number)
                try:
                    key = int(ref)
                except ValueError:
                    key = gl.line_number
                grn_qty_map[key] = grn_qty_map.get(key, 0) + gl.quantity_received

        for inv_line in invoice.line_items:
            # Find matching PO line
            po_line = self._find_po_line(inv_line, po.line_items)

            if not po_line:
                results.append(LineMatchResult(
                    invoice_line=inv_line.line_number,
                    status=MatchStatus.UNMATCHED,
                    quantity_invoice=inv_line.quantity,
                    price_invoice=inv_line.unit_price,
                    notes=["No matching PO line found"],
                ))
                continue

            # Quantity comparison
            qty_variance_pct = 0.0
            qty_match = True
            if po_line.quantity:
                qty_variance_pct = round(
                    (inv_line.quantity - po_line.quantity) / po_line.quantity * 100, 2
                )
                qty_match = abs(qty_variance_pct) <= self.policy.qty_tolerance_pct

            # Price comparison
            price_variance_pct = 0.0
            price_match = True
            if po_line.unit_price:
                price_variance_pct = round(
                    (inv_line.unit_price - po_line.unit_price) / po_line.unit_price * 100, 2
                )
                price_match = abs(price_variance_pct) <= self.policy.price_tolerance_pct

            # GRN quantity for 3-way
            grn_line_num = po_line.line_number
            grn_qty = grn_qty_map.get(grn_line_num)

            notes = []
            if not qty_match:
                notes.append(f"Qty variance: {qty_variance_pct}%")
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.QUANTITY_VARIANCE,
                    severity=Severity.WARNING if abs(qty_variance_pct) <= self.policy.qty_tolerance_pct * 2 else Severity.ERROR,
                    confidence=1.0,
                    title=f"Line {inv_line.line_number}: quantity variance {qty_variance_pct}%",
                    description=(
                        f"Invoice qty ({inv_line.quantity}) vs PO qty ({po_line.quantity}): "
                        f"{qty_variance_pct}% variance (tolerance: ±{self.policy.qty_tolerance_pct}%)"
                    ),
                    data={"line": inv_line.line_number, "invoice_qty": inv_line.quantity,
                          "po_qty": po_line.quantity, "variance_pct": qty_variance_pct},
                ))

            if not price_match:
                notes.append(f"Price variance: {price_variance_pct}%")
                self.add_finding(Finding(
                    agent=self.name,
                    category=ExceptionCategory.PRICE_VARIANCE,
                    severity=Severity.WARNING if abs(price_variance_pct) <= self.policy.price_tolerance_pct * 2 else Severity.ERROR,
                    confidence=1.0,
                    title=f"Line {inv_line.line_number}: price variance {price_variance_pct}%",
                    description=(
                        f"Invoice price ({inv_line.unit_price}) vs PO price ({po_line.unit_price}): "
                        f"{price_variance_pct}% variance (tolerance: ±{self.policy.price_tolerance_pct}%)"
                    ),
                    data={"line": inv_line.line_number, "invoice_price": inv_line.unit_price,
                          "po_price": po_line.unit_price, "variance_pct": price_variance_pct},
                ))

            if match_type == MatchType.THREE_WAY and grn_qty is not None:
                if abs(inv_line.quantity - grn_qty) > 0.01:
                    notes.append(f"GRN qty mismatch: invoice={inv_line.quantity}, received={grn_qty}")

            status = MatchStatus.MATCHED
            if not qty_match or not price_match:
                status = MatchStatus.MISMATCHED
            elif notes:
                status = MatchStatus.PARTIAL

            results.append(LineMatchResult(
                invoice_line=inv_line.line_number,
                po_line=po_line.line_number,
                grn_line=grn_line_num if grn_qty is not None else None,
                quantity_match=qty_match,
                price_match=price_match,
                quantity_variance_pct=qty_variance_pct,
                price_variance_pct=price_variance_pct,
                quantity_invoice=inv_line.quantity,
                quantity_po=po_line.quantity,
                quantity_grn=grn_qty,
                price_invoice=inv_line.unit_price,
                price_po=po_line.unit_price,
                status=status,
                notes=notes,
            ))

        return results

    def _find_po_line(self, inv_line, po_lines: list[POLineItem]) -> POLineItem | None:
        # Match by line number first
        if inv_line.po_line_ref:
            try:
                ref_num = int(inv_line.po_line_ref)
                for pl in po_lines:
                    if pl.line_number == ref_num:
                        return pl
            except ValueError:
                pass

        # Match by line number
        for pl in po_lines:
            if pl.line_number == inv_line.line_number:
                return pl

        # Match by description similarity
        from rapidfuzz import fuzz
        best_match = None
        best_score = 0
        for pl in po_lines:
            score = fuzz.ratio(inv_line.description.lower(), pl.description.lower())
            if score > best_score:
                best_score = score
                best_match = pl
        if best_score >= 60:
            return best_match
        return None

    def _compute_overall_status(self, line_matches: list[LineMatchResult]) -> MatchStatus:
        if not line_matches:
            return MatchStatus.UNMATCHED
        statuses = [lm.status for lm in line_matches]
        if all(s == MatchStatus.MATCHED for s in statuses):
            return MatchStatus.MATCHED
        if any(s == MatchStatus.MISMATCHED for s in statuses):
            return MatchStatus.MISMATCHED
        if any(s == MatchStatus.UNMATCHED for s in statuses):
            return MatchStatus.PARTIAL
        return MatchStatus.PARTIAL

    def _build_summary(self, status: MatchStatus, match_type: MatchType, lines: list[LineMatchResult]) -> str:
        matched = sum(1 for l in lines if l.status == MatchStatus.MATCHED)
        total = len(lines)
        return f"{match_type.value} matching: {matched}/{total} lines matched. Overall: {status.value}"