# Invoice PDFs

Synthetic invoice PDFs for single-file CLI testing.

## Folders

- `data_inputs/invoice_pdfs/clean/`
- `data_inputs/invoice_pdfs/duplicate/`
- `data_inputs/invoice_pdfs/high_value/`
- `data_inputs/invoice_pdfs/malformed/`
- `data_inputs/invoice_pdfs/no_po/`

## Files

- `clean/manufacturing_goods_invoice.pdf`
- `duplicate/telecom_monthly_invoice.pdf`
- `high_value/logistics_freight_invoice.pdf`
- `malformed/malformed_invoice.pdf`
- `no_po/consulting_services_invoice.pdf`

## Run examples

```bash
python -m src.cli process data_inputs/invoice_pdfs/clean/manufacturing_goods_invoice.pdf --output manual_runs
python -m src.cli process data_inputs/invoice_pdfs/no_po/consulting_services_invoice.pdf --output manual_runs
python -m src.cli process data_inputs/invoice_pdfs/high_value/logistics_freight_invoice.pdf --output manual_runs
```
