from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> dict:
    """Load a YAML file and return its contents as a dict."""
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def save_json(
    data: Any,
    path: str | Path,
    indent: int = 2,
    mask_config: dict[str, bool] | None = None,
) -> None:
    """Save data as JSON to the given path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if hasattr(data, "model_dump"):
        serializable = data.model_dump()
    else:
        serializable = data

    if mask_config and mask_config.get("mask_sensitive_artifacts", False):
        serializable = mask_sensitive_data(serializable, mask_config)

    with open(path, "w") as f:
        json.dump(serializable, f, indent=indent, default=str)


def load_json(path: str | Path) -> dict | list:
    """Load a JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def save_csv(
    rows: list[dict],
    path: str | Path,
    mask_config: dict[str, bool] | None = None,
) -> None:
    """Save a list of dicts as CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with open(path, "w") as f:
            f.write("")
        return
    if mask_config and mask_config.get("mask_sensitive_artifacts", False):
        rows = [mask_sensitive_data(row, mask_config) for row in rows]
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def save_markdown(
    content: str,
    path: str | Path,
    mask_config: dict[str, bool] | None = None,
) -> None:
    """Save markdown content to a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mask_config and mask_config.get("mask_sensitive_artifacts", False):
        content = mask_sensitive_text(content, mask_config)
    with open(path, "w") as f:
        f.write(content)


def ensure_run_dir(base: str | Path, run_id: str) -> Path:
    """Create and return the run directory."""
    run_dir = Path(base) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def list_files(directory: str | Path, extensions: list[str] | None = None) -> list[Path]:
    """List files in a directory, optionally filtered by extension."""
    directory = Path(directory)
    if not directory.exists():
        return []
    files = [f for f in directory.iterdir() if f.is_file()]
    if extensions:
        ext_set = {e.lower().lstrip(".") for e in extensions}
        files = [f for f in files if f.suffix.lower().lstrip(".") in ext_set]
    return sorted(files)


def mask_sensitive_data(data: Any, mask_config: dict[str, bool] | None = None) -> Any:
    """Recursively mask sensitive values in structured objects."""
    mask_config = mask_config or {}
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for key, value in data.items():
            key_l = str(key).lower()
            if _should_mask_bank_key(key_l, mask_config):
                masked[key] = _mask_value(value)
            elif _should_mask_tax_key(key_l, mask_config):
                masked[key] = _mask_value(value)
            else:
                masked[key] = mask_sensitive_data(value, mask_config)
        return masked
    if isinstance(data, list):
        return [mask_sensitive_data(item, mask_config) for item in data]
    return data


def mask_sensitive_text(text: str, mask_config: dict[str, bool] | None = None) -> str:
    """Mask sensitive key/value patterns in unstructured log text."""
    mask_config = mask_config or {}
    masked_text = text

    if mask_config.get("mask_bank_details_in_logs", False):
        masked_text = re.sub(
            r"(?i)\b(bank(?:[_\s-]?account)?|account(?:[_\s-]?number)?|iban|routing|swift)\b(\s*[:=]\s*)([A-Za-z0-9\-]{4,})",
            lambda m: f"{m.group(1)}{m.group(2)}{_mask_value(m.group(3))}",
            masked_text,
        )

    if mask_config.get("mask_tax_ids_in_logs", False):
        masked_text = re.sub(
            r"(?i)\b(tax(?:[_\s-]?id)|vat(?:[_\s-]?id|[_\s-]?number)?|ein|tin)\b(\s*[:=]\s*)([A-Za-z0-9\-]{4,})",
            lambda m: f"{m.group(1)}{m.group(2)}{_mask_value(m.group(3))}",
            masked_text,
        )

    return masked_text


def _mask_value(value: Any) -> Any:
    if value is None:
        return value
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def _should_mask_bank_key(key: str, mask_config: dict[str, bool]) -> bool:
    if not mask_config.get("mask_bank_details_in_logs", False):
        return False
    parts = _split_key_tokens(key)
    if _contains_token_sequence(parts, ("bank", "account")):
        # Keep non-sensitive metadata fields visible, such as change dates.
        if _contains_token_sequence(parts, ("last", "changed")):
            return False
        return True
    if _contains_token_sequence(parts, ("account", "number")):
        return True
    return any(token in parts for token in ("iban", "routing", "swift"))


def _should_mask_tax_key(key: str, mask_config: dict[str, bool]) -> bool:
    if not mask_config.get("mask_tax_ids_in_logs", False):
        return False
    parts = _split_key_tokens(key)
    if _contains_token_sequence(parts, ("tax", "id")):
        return True
    if _contains_token_sequence(parts, ("vat", "id")):
        return True
    if _contains_token_sequence(parts, ("vat", "number")):
        return True
    return any(token in parts for token in ("ein", "tin"))


def _split_key_tokens(key: str) -> list[str]:
    return [p for p in re.split(r"[^a-z0-9]+", key.lower()) if p]


def _contains_token_sequence(parts: list[str], seq: tuple[str, ...]) -> bool:
    if len(parts) < len(seq):
        return False
    for i in range(len(parts) - len(seq) + 1):
        if tuple(parts[i : i + len(seq)]) == seq:
            return True
    return False
