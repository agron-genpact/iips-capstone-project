
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.file_utils import load_yaml

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "policy.yaml"


class Policy:
    """Typed wrapper around the policy YAML configuration."""

    def __init__(self, path: str | Path | None = None):
        self._data = load_yaml(path or _DEFAULT_POLICY_PATH)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Get a nested value using dot notation, e.g. 'tolerance.quantity_percent'."""
        keys = dotted_key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    # ── Shortcuts ──────────────────────────────────────────────────────

    @property
    def auto_approve_max(self) -> float:
        return float(self.get("approval_thresholds.auto_approve_max", 5000))

    @property
    def manager_approval_max(self) -> float:
        return float(self.get("approval_thresholds.manager_approval_max", 50000))

    @property
    def qty_tolerance_pct(self) -> float:
        return float(self.get("tolerance.quantity_percent", 5.0))

    @property
    def price_tolerance_pct(self) -> float:
        return float(self.get("tolerance.price_percent", 2.0))

    @property
    def total_tolerance_pct(self) -> float:
        return float(self.get("tolerance.total_percent", 1.0))

    @property
    def absolute_max_tolerance(self) -> float:
        return float(self.get("tolerance.absolute_max", 50.0))

    @property
    def require_grn_for_goods(self) -> bool:
        return bool(self.get("matching.require_grn_for_goods", True))

    @property
    def po_required(self) -> bool:
        return bool(self.get("matching.po_required", True))

    @property
    def default_tax_rate(self) -> float:
        return float(self.get("compliance.default_tax_rate", 18.0))

    @property
    def tax_rate_tolerance(self) -> float:
        return float(self.get("compliance.tax_rate_tolerance", 0.5))

    @property
    def tax_validation_enabled(self) -> bool:
        return bool(self.get("compliance.tax_validation_enabled", True))

    @property
    def allowed_currencies(self) -> list[str]:
        return self.get("compliance.allowed_currencies", ["USD"])

    @property
    def duplicate_similarity_threshold(self) -> float:
        return float(self.get("duplicate_detection.similarity_threshold", 0.85))

    @property
    def duplicate_lookback_days(self) -> int:
        return int(self.get("duplicate_detection.lookback_days", 90))

    @property
    def vendor_fuzzy_threshold(self) -> int:
        return int(self.get("vendor.fuzzy_match_threshold", 80))

    @property
    def min_ocr_confidence(self) -> float:
        return float(self.get("ocr.min_confidence", 0.7))

    @property
    def anomaly_bank_change_days(self) -> int:
        return int(self.get("anomaly_detection.bank_change_lookback_days", 30))

    @property
    def anomaly_just_under_pct(self) -> float:
        return float(self.get("anomaly_detection.just_under_threshold_percent", 5.0))