from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from src.schemas.models import DocumentType, EvidencePointer, Finding, Severity
from src.utils.file_utils import mask_sensitive_text
from src.utils.policy import Policy

logger = logging.getLogger("iips")


class BaseAgent(ABC):
    """Abstract base for all pipeline agents."""

    name: str = "base"

    def __init__(self, run_dir: Path, policy: Policy):
        self.run_dir = run_dir
        self.policy = policy
        self.findings: list[Finding] = []
        self.audit_entries: list[str] = []
        self._default_evidence_source: str | None = None

    def log(self, message: str) -> None:
        """Add an audit log entry."""
        masked_message = mask_sensitive_text(message, self.policy.privacy_mask_config)
        entry = f"[{self.name}] {masked_message}"
        self.audit_entries.append(entry)
        logger.info(entry)

    def add_finding(self, finding: Finding) -> None:
        """Record a finding."""
        if finding.severity in (Severity.CRITICAL, Severity.ERROR) and not finding.evidence:
            source = self._default_evidence_source or f"{self.name}:context"
            finding.evidence.append(EvidencePointer(
                source_file=source,
                field=finding.category.value,
            ))
        self.findings.append(finding)
        self.log(f"Finding [{finding.severity.value}]: {finding.title}")

    def set_default_evidence_source(self, source_file: str | None) -> None:
        self._default_evidence_source = source_file

    def set_default_evidence_from_context(self, context: dict[str, Any]) -> None:
        packet = context.get("context_packet")
        if not packet or not hasattr(packet, "documents"):
            return
        for doc in packet.documents:
            if doc.document_type == DocumentType.INVOICE:
                self._default_evidence_source = doc.file_path
                return

    @abstractmethod
    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute the agent's logic. Returns updated context."""
        ...
