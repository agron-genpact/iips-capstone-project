# Intelligent Invoice Processing System (IIPS)

## Virtual Environment Setup

Using a virtual environment keeps project dependencies isolated.

### 1. Go to the project folder

```bash
cd iips-capstone-project
```

### 2. Create a virtual environment

#### Windows (PowerShell)

```powershell
python -m venv .venv
```

#### macOS (Terminal)

```bash
python3 -m venv .venv
```

### 3. Activate the virtual environment

#### Windows (PowerShell)

```powershell
.\.venv\Scripts\Activate.ps1
```

#### macOS (Terminal)

```bash
source .venv/bin/activate
```

### 4. Verify activation

You should see `(.venv)` at the start of your terminal prompt.

### 5. Deactivate when done

```bash
deactivate
```

## Install Python Requirements

Install project dependencies from `requirements.txt`:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Tesseract OCR Engine (Required for Image OCR)

Installing `pytesseract` with pip is not enough by itself. You must also install the Tesseract system executable.

## CLI Usage

### Process Commands (and output artifacts)

```bash
python -m src.cli process data_inputs/bundles/clean_invoice
python -m src.cli process data_inputs/bundles/bank_change_anomaly
python -m src.cli process data_inputs/invoice_pdfs/duplicate/telecom_monthly_invoice.pdf -o runs
```

What each command does:

- `clean_invoice`: processes a clean 3-way-match bundle and typically ends with `AUTO_POST`.
- `bank_change_anomaly`: processes a risky bundle (bank account mismatch + anomaly signals) and typically ends with `HOLD`.
- `telecom_monthly_invoice.pdf`: processes a single PDF invoice file (not a full bundle). It typically ends with `HOLD` because duplicate/missing-master-data findings are raised.

Where output is written:

- Default output root is `runs`, and run artifacts are created under `runs/runs/{run_id}`.
- If you pass `-o <dir>`, artifacts go under `<dir>/runs/{run_id}`.

Typical artifact files in each run folder:

- `context_packet.json`
- `extracted_invoice.json`
- `line_items.csv`
- `vendor_resolution.json`
- `validation_result.json`
- `match_result.json`
- `compliance_result.json`
- `anomaly_result.json`
- `approval_packet.json`
- `final_decision.json`
- `posting_payload.json`
- `exceptions.md`
- `audit_log.md`
- `metrics.json`

Note: single-file PDF processing can produce one fewer artifact (for example, no `vendor_resolution.json` if no vendor master file is present).

### Test + Run Listing Commands

```bash
python -m pytest test/test_pipeline.py
python -m src.cli list -o test_runs
```

What they do:

- `pytest`: runs the pipeline test suite (bundle behavior, decision logic, artifact generation, policy behavior, and CLI behavior).
- `src.cli list -o test_runs`: lists all saved runs found in `test_runs/runs/*`, showing `Run ID`, `Decision`, and artifact count.

Bundle outcomes with default policy (from current sample bundles):

- `bank_change_anomaly` -> `HOLD`
- `clean_invoice` -> `AUTO_POST`
- `clean_small_invoice` -> `AUTO_POST`
- `credit_note` -> `ROUTE_FOR_APPROVAL`
- `duplicate_invoice` -> `HOLD`
- `header_mismatch` -> `ROUTE_FOR_APPROVAL`
- `low_ocr_confidence` -> `ROUTE_FOR_APPROVAL`
- `multi_currency` -> `ROUTE_FOR_APPROVAL`
- `new_vendor` -> `ROUTE_FOR_APPROVAL`
- `no_grn` -> `ROUTE_FOR_APPROVAL`
- `no_po_invoice` -> `ROUTE_FOR_APPROVAL`
- `price_variance` -> `ROUTE_FOR_APPROVAL`
- `quantity_variance` -> `ROUTE_FOR_APPROVAL`
- `split_deliveries` -> `APPROVE_AND_POST`
- `tax_mismatch` -> `ROUTE_FOR_APPROVAL`

### Inspect a Previous Run

```bash
python -m src.cli inspect test_runs/runs/{run_id}
```

This command prints:

- the artifact tree (file names + sizes) in that run directory
- a final decision summary table (`decision`, `reason`, `risk score`, `invoice`, `vendor`, `amount`)

### Policy Comparison Commands

```bash
python -m src.cli process data_inputs/bundles/price_variance -o policy_runs
python -m src.cli process data_inputs/bundles/price_variance -o policy_runs -p config/policy_loose_price_variance.yaml
```

For `price_variance`, different policy gives different output:

- Default policy run: `ROUTE_FOR_APPROVAL`
- Loose policy run (`policy_loose_price_variance.yaml`): `APPROVE_AND_POST`

Key differences observed:

- `match_result.overall_status`: `mismatched` -> `matched`
- `within_tolerance`: `false` -> `true`
- Findings reduced from 3 to 1 (price/total variance errors removed)
- Risk score reduced from `4.5` -> `0.5`

```bash
python -m src.cli process data_inputs/bundles/no_grn -o policy_runs
python -m src.cli process data_inputs/bundles/no_grn -o policy_runs -p config/policy_loose_no_grn.yaml -v
```

For `no_grn`, different policy also changes result:

- Default policy run: `ROUTE_FOR_APPROVAL`
- Loose policy run (`policy_loose_no_grn.yaml`): `APPROVE_AND_POST`

Why this changes:

- Default policy requires GRN for goods (`require_grn_for_goods: true`), so missing GRN is an error.
- Loose policy sets `require_grn_for_goods: false`, so the GRN error is removed.
- `-v` enables verbose agent-by-agent logs during processing.

## Streamlit App

Run the browser UI:

```bash
streamlit run app.py
```

What you can do in the UI:

- Upload and process a single invoice file (PDF/Image/JSON/YAML)
- Run the pipeline against an existing local bundle or file path
- Inspect previous runs under `ui_runs/runs/*`
- Preview and download generated artifacts
