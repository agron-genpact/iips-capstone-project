from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.pipeline import Pipeline
from src.utils.file_utils import load_json


def save_uploaded_file(uploaded_file: Any, uploads_root: str | Path = ".ui_inputs") -> Path:
    """Persist a Streamlit uploaded file and return the saved path."""
    uploads_dir = Path(uploads_root)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    safe_name = Path(uploaded_file.name).name
    out_path = uploads_dir / f"{ts}_{safe_name}"
    out_path.write_bytes(uploaded_file.getbuffer())
    return out_path


def run_pipeline(
    input_path: str | Path,
    output_root: str | Path = "ui_runs",
    policy_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Run IIPS pipeline and return context + run directory."""
    pipeline = Pipeline(
        bundle_path=str(input_path),
        output_dir=str(output_root),
        policy_path=str(policy_path) if policy_path else None,
    )
    context = pipeline.run()
    if pipeline.run_dir is None:
        raise RuntimeError("Pipeline completed without producing a run directory.")
    return context, pipeline.run_dir


def list_runs(output_root: str | Path = "ui_runs") -> list[Path]:
    """List run directories newest-first for the given output root."""
    runs_dir = Path(output_root) / "runs"
    if not runs_dir.exists():
        return []
    runs = [p for p in runs_dir.iterdir() if p.is_dir()]
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return runs


def load_json_artifact(run_dir: str | Path, filename: str) -> dict | list:
    """Load a JSON artifact from a run directory."""
    return load_json(Path(run_dir) / filename)


def available_artifacts(run_dir: str | Path) -> list[Path]:
    run_path = Path(run_dir)
    if not run_path.exists():
        return []
    return sorted([p for p in run_path.iterdir() if p.is_file()])
