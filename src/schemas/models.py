from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

class DocumentType(str, Enum):
    INVOICE = "invoice"
    PURCHASE_ORDER = "purchase_order"
    GRN = "goods_receipt_note"
    CREDIT_NOTE = "credit_note"
    VENDOR_MASTER = "vendor_master"
    TAX_RULES = "tax_rules"
    APPROVAL_POLICY = "approval_policy"
    UNKNOWN = "unknown"

class EvidencePointer(BaseModel):
    source_file: str
    page: Optional[int] = None
    field: Optional[str] = None
    bbox: Optional[list[float]] = None
    text_snippet: Optional[str] = None

class DocumentEntry(BaseModel):
    file_path: str
    document_type: DocumentType
    metadata: dict[str, Any] = Field(default_factory=dict)

class ContextPacket(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    bundle_path: str
    documents: list[DocumentEntry] = Field(default_factory=list)
    vendor_candidates: list[str] = Field(default_factory=list)
    po_references: list[str] = Field(default_factory=list)
    grn_references: list[str] = Field(default_factory=list)
    risk_indicators: list[str] = Field(default_factory=list)
    evidence_index: list[EvidencePointer] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)