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
