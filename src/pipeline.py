from __future__ import annotations

import json
import logging
from hashlib import sha1
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.agent_a_intake import IntakeAgent
from src.agents.agent_b_extraction import ExtractionAgent
from src.agents.agent_c_vendor import VendorResolutionAgent
from src.agents.agent_d_validation import ValidationAgent
from src.agents.agent_e_matching import MatchingAgent
from src.agents.agent_f_compliance import ComplianceAgent
from src.agents.agent_g_anomaly import AnomalyDetectionAgent
from src.agents.agent_h_exception import ExceptionTriageAgent
from src.agents.agent_i_orchestrator import OrchestratorAgent
from src.utils.file_utils import ensure_run_dir
from src.utils.policy import Policy

logger = logging.getLogger("iips")


class Pipeline:
    """Runs the full invoice processing pipeline."""

    def __init__(
        self,
        bundle_path: str | Path,
        output_dir: str | Path = "runs",
        policy_path: str | Path | None = None,
    ):
        self.bundle_path = Path(bundle_path)
        self.output_dir = Path(output_dir)
        self.policy_path = Path(policy_path) if policy_path else None
        self.policy = Policy(policy_path)
        self.run_dir: Path | None = None

    def run(self) -> dict[str, Any]:
        """Execute the full pipeline and return the final context."""
        if self.policy.strict_reproducibility:
            start_time = "2000-01-01T00:00:00+00:00"
        else:
            start_time = datetime.now(timezone.utc).isoformat()

        # Initialize context
        invoice_history, invoice_history_source = self._load_invoice_history()
        context: dict[str, Any] = {
            "bundle_path": str(self.bundle_path),
            "start_time": start_time,
            "strict_reproducibility": self.policy.strict_reproducibility,
            "all_findings": [],
            "audit_entries": [],
            "invoice_history": invoice_history,
            "invoice_history_source": invoice_history_source,
        }

        # Agent pipeline in order
        agents_classes = [
            IntakeAgent,         # A: Intake & Context
            ExtractionAgent,     # B: OCR & Extraction
            VendorResolutionAgent,  # C: Vendor Resolution
            ValidationAgent,     # D: Validation
            MatchingAgent,       # E: Matching
            ComplianceAgent,     # F: Compliance
            AnomalyDetectionAgent,  # G: Anomaly Detection
        ]

        # Create run dir before any agent runs
        run_id = self._generate_run_id()
        context["run_id"] = run_id
        self.run_dir = ensure_run_dir(self.output_dir, run_id)

        # Phase 1: Run all analysis agents
        for agent_cls in agents_classes:
            agent = agent_cls(run_dir=self.run_dir, policy=self.policy)

            logger.info(f"Running {agent.name}...")
            try:
                context = agent.run(context)
            except Exception as e:
                logger.error(f"Agent {agent.name} failed: {e}")
                context["audit_entries"].append(f"[{agent.name}] FAILED: {e}")
                raise

            # Collect findings and audit entries
            context["all_findings"].extend(agent.findings)
            context["audit_entries"].extend(agent.audit_entries)

            # Apply bundle approval-policy overrides immediately after intake.
            if agent_cls is IntakeAgent:
                overrides = context.get("policy_overrides")
                if overrides:
                    self.policy.apply_overrides(overrides)
                    context["audit_entries"].append("[pipeline] Applied bundle policy overrides")

        # Phase 2: Exception triage
        triage = ExceptionTriageAgent(run_dir=self.run_dir, policy=self.policy)
        context = triage.run(context)
        context["all_findings"].extend(triage.findings)
        context["audit_entries"].extend(triage.audit_entries)

        # Phase 3: Final orchestration
        orchestrator = OrchestratorAgent(run_dir=self.run_dir, policy=self.policy)
        context = orchestrator.run(context)
        context["audit_entries"].extend(orchestrator.audit_entries)

        logger.info(f"Pipeline complete. Run ID: {run_id}")
        logger.info(f"Artifacts in: {self.run_dir}")

        return context

    def _load_invoice_history(self) -> tuple[list[dict], str | None]:
        """Load invoice history from bundle if present (for duplicate detection)."""
        history_file = (
            self.bundle_path.parent / "invoice_history.json"
            if self.bundle_path.is_file()
            else self.bundle_path / "invoice_history.json"
        )
        if history_file.exists():
            with open(history_file) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data, str(history_file)
                return [data], str(history_file)
        return [], None

    def _generate_run_id(self) -> str:
        if not self.policy.strict_reproducibility:
            import uuid
            return str(uuid.uuid4())[:12]

        policy_marker = str(self.policy_path.resolve()) if self.policy_path else "default-policy"
        seed = f"{self.bundle_path.resolve()}::{policy_marker}"
        return sha1(seed.encode("utf-8")).hexdigest()[:12]
