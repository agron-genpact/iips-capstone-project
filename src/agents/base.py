from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from src.schemas.models import Finding
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

    def log(self, message: str) -> None:
        """Add an audit log entry."""
        entry = f"[{self.name}] {message}"
        self.audit_entries.append(entry)
        logger.info(entry)

    def add_finding(self, finding: Finding) -> None:
        """Record a finding."""
        self.findings.append(finding)
        self.log(f"Finding [{finding.severity.value}]: {finding.title}")

    @abstractmethod
    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute the agent's logic. Returns updated context."""
        ...