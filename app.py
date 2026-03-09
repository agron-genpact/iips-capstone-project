from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from src.ui.service import (
    available_artifacts,
    list_runs,
    load_json_artifact,
    run_pipeline,
    save_uploaded_file,
)


def _decision_color(decision: str) -> str:
    mapping = {
        "auto_post": "green",
        "approve_and_post": "blue",
        "route_for_approval": "orange",
        "hold": "red",
        "reject": "red",
        "manual_review": "orange",
    }
    return mapping.get(decision, "gray")


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {}


def _render_decision(context: dict[str, Any], run_dir: Path) -> None:
    decision_obj = _as_dict(context.get("final_decision"))
    if not decision_obj:
        st.warning("No final decision found in pipeline context.")
        return

    decision = str(decision_obj.get("decision", "unknown"))
    reason = str(decision_obj.get("reason", "No reason available."))
    color = _decision_color(decision)

    st.markdown(
        f"### Final Decision: :{color}[{decision.upper()}]\n\n"
        f"**Reason:** {reason}\n\n"
        f"**Run Directory:** `{run_dir}`"
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Invoice", decision_obj.get("invoice_number") or "N/A")
    col2.metric("Vendor", decision_obj.get("vendor_name") or "N/A")
    col3.metric("Amount", f"{decision_obj.get('currency', 'USD')} {decision_obj.get('total_amount', 'N/A')}")
    col4.metric("Risk Score", decision_obj.get("risk_score", "N/A"))

    findings = decision_obj.get("all_findings", []) or []
    st.write(f"Total Findings: **{len(findings)}**")
    if findings:
        rows = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            rows.append(
                {
                    "severity": finding.get("severity", ""),
                    "category": finding.get("category", ""),
                    "agent": finding.get("agent", ""),
                    "title": finding.get("title", ""),
                }
            )
        st.dataframe(rows, use_container_width=True)


def _render_artifacts(run_dir: Path) -> None:
    st.subheader("Artifacts")
    artifacts = available_artifacts(run_dir)
    if not artifacts:
        st.info("No artifacts found.")
        return

    for artifact in artifacts:
        with st.expander(artifact.name, expanded=False):
            if artifact.suffix.lower() == ".json":
                try:
                    st.json(load_json_artifact(run_dir, artifact.name))
                except Exception as exc:
                    st.error(f"Failed to load JSON: {exc}")
            elif artifact.suffix.lower() in (".md", ".txt", ".csv"):
                try:
                    st.code(artifact.read_text(), language="markdown")
                except Exception as exc:
                    st.error(f"Failed to read text artifact: {exc}")
            else:
                st.write(f"Binary file: `{artifact.name}`")

            try:
                data = artifact.read_bytes()
                st.download_button(
                    label=f"Download {artifact.name}",
                    data=data,
                    file_name=artifact.name,
                    mime="application/octet-stream",
                    key=f"dl-{artifact.name}-{artifact.stat().st_mtime_ns}",
                )
            except Exception:
                pass


def main() -> None:
    st.set_page_config(page_title="IIPS Streamlit UI", layout="wide")
    st.title("Intelligent Invoice Processing System (IIPS)")
    st.caption("Run invoice pipeline from a browser UI and inspect artifacts.")

    with st.sidebar:
        st.header("Run Settings")
        output_root = st.text_input("Output root", value="ui_runs")
        policy_path = st.text_input("Policy file (optional)", value="")
        mode = st.radio("Input mode", ("Upload file", "Existing path"))

        st.header("Inspect Runs")
        refresh = st.button("Refresh runs")
        if refresh:
            st.rerun()

        runs = list_runs(output_root)
        selected_run = st.selectbox(
            "Previous runs",
            options=[""] + [r.name for r in runs],
            index=0,
        )

    tab_run, tab_inspect = st.tabs(["Run Pipeline", "Inspect Run"])

    with tab_run:
        input_path: Path | None = None
        if mode == "Upload file":
            uploaded = st.file_uploader(
                "Upload invoice file",
                type=["pdf", "png", "jpg", "jpeg", "tiff", "tif", "bmp", "json", "yaml", "yml"],
            )
            if uploaded:
                st.write(f"Selected upload: `{uploaded.name}`")
            if st.button("Run uploaded file", type="primary", disabled=uploaded is None):
                if uploaded is None:
                    st.error("Upload a file first.")
                else:
                    try:
                        input_path = save_uploaded_file(uploaded)
                        with st.spinner("Running pipeline..."):
                            context, run_dir = run_pipeline(
                                input_path=input_path,
                                output_root=output_root,
                                policy_path=policy_path.strip() or None,
                            )
                        st.success("Pipeline run completed.")
                        _render_decision(context, run_dir)
                        _render_artifacts(run_dir)
                    except Exception as exc:
                        st.error(f"Pipeline failed: {exc}")
        else:
            bundle_or_file = st.text_input("Bundle or file path", value="data_inputs/bundles/clean_invoice")
            if st.button("Run path", type="primary"):
                try:
                    input_path = Path(bundle_or_file)
                    if not input_path.exists():
                        st.error(f"Path not found: {input_path}")
                    else:
                        with st.spinner("Running pipeline..."):
                            context, run_dir = run_pipeline(
                                input_path=input_path,
                                output_root=output_root,
                                policy_path=policy_path.strip() or None,
                            )
                        st.success("Pipeline run completed.")
                        _render_decision(context, run_dir)
                        _render_artifacts(run_dir)
                except Exception as exc:
                    st.error(f"Pipeline failed: {exc}")

    with tab_inspect:
        if not selected_run:
            st.info("Choose a run from the sidebar to inspect artifacts.")
        else:
            run_dir = Path(output_root) / "runs" / selected_run
            st.write(f"Inspecting: `{run_dir}`")
            final_decision_path = run_dir / "final_decision.json"
            if final_decision_path.exists():
                try:
                    context = {"final_decision": json.loads(final_decision_path.read_text())}
                    _render_decision(context, run_dir)
                except Exception as exc:
                    st.error(f"Failed to parse final decision: {exc}")
            _render_artifacts(run_dir)


if __name__ == "__main__":
    main()
