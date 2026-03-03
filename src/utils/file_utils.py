from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> dict:
    """Load a YAML file and return its contents as a dict."""
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def save_json(data: Any, path: str | Path, indent: int = 2) -> None:
    """Save data as JSON to the given path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        if hasattr(data, "model_dump"):
            json.dump(data.model_dump(), f, indent=indent, default=str)
        else:
            json.dump(data, f, indent=indent, default=str)


def load_json(path: str | Path) -> dict | list:
    """Load a JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def save_csv(rows: list[dict], path: str | Path) -> None:
    """Save a list of dicts as CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with open(path, "w") as f:
            f.write("")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def save_markdown(content: str, path: str | Path) -> None:
    """Save markdown content to a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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