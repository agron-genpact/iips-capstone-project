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

Process either a full bundle directory or a single invoice file:

```bash
python -m src.cli process data_inputs/bundles/clean_invoice
python -m src.cli process /path/to/invoice.pdf
python -m src.cli process /path/to/invoice.png
```

List completed runs from `<output>/runs/*`:

```bash
python -m src.cli list --output runs
```

## Streamlit UI

Run the browser UI:

```bash
streamlit run app.py
```

What you can do in the UI:

- Upload and process a single invoice file (PDF/Image/JSON/YAML)
- Run the pipeline against an existing local bundle or file path
- Inspect previous runs under `ui_runs/runs/*`
- Preview and download generated artifacts

## Startup Preflight Checks

`process` now runs dependency preflight checks before execution and prints actionable messages.

- For PDF inputs: checks `pdfplumber`
- For image inputs: checks Python OCR deps (`pytesseract`, `Pillow`) and the `tesseract` binary on `PATH`

If Tesseract is missing, processing continues but OCR confidence findings will be raised and extraction quality will degrade.

### Install Tesseract

- macOS (Homebrew): `brew install tesseract`
- Ubuntu/Debian: `sudo apt-get install tesseract-ocr`

Verify installation:

```bash
tesseract --version
```
