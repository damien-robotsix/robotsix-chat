## File-hub tools — document fetch, fill, and upload

You have four tools for working with files via the robotsix-file-hub service:

### `file_hub_get` — download a file from file-hub

Downloads a file by its file-hub UUID to a local working directory and returns the local path plus
metadata (filename, size, content-type, category, tags, summary).

**Use when:** you need to inspect, process, or fill a document that was pushed to file-hub (e.g. a
PDF attachment from an email).

**Example:** `file_hub_get(file_id="e0367d94-1895-4756-8730-3867f694fd05")`

### `list_pdf_form_fields` — inspect fillable fields in a PDF

Lists all AcroForm form fields in a PDF: field names, types, current values, and available options
(for dropdowns/checkboxes). Use this before `fill_pdf_document` to discover what fields are
available.

**Use when:** you have a PDF and need to know what form fields it contains before filling them.

**Example:** `list_pdf_form_fields(pdf_path="/data/file_hub_work/form.pdf")`

### `fill_pdf_document` — fill a PDF form or overlay text

Fills a PDF in one or both of two modes:

1. **Form-field fill** — for PDFs with AcroForm fields. Pass `field_values` as a JSON object mapping
   field names to values: `{"name": "John Doe", "date": "2025-01-15"}`

1. **Text overlay** — for flat/non-form PDFs. Pass `text_overlays` as a JSON array:
   `[{"page": 0, "x": 100, "y": 700, "text": "John Doe", "font_size": 12}]`

Coordinates are in PDF points (72 per inch) from the bottom-left corner of the page. Both modes can
be combined in a single call.

**Constraints:**

- **No signature forging.** This tool writes text and field values only. It must not attempt to
  reproduce handwritten signatures. The human operator signs documents manually after downloading
  the filled version.
- Use `list_pdf_form_fields` first to discover available field names.

**Example (form fill):**

```text
fill_pdf_document(
    pdf_path="/data/file_hub_work/sepa_mandate.pdf",
    field_values='{"account_holder": "Jean Dupont", "iban": "FR76...", "bic": "BNPAFRPP"}',
    output_path="/data/file_hub_work/sepa_mandate_filled.pdf"
)
```

**Example (overlay):**

```text
fill_pdf_document(
    pdf_path="/data/file_hub_work/flat_form.pdf",
    text_overlays='[{"page": 0, "x": 150, "y": 680, "text": "Jean Dupont"}]',
    output_path="/data/file_hub_work/flat_form_filled.pdf"
)
```

### `file_hub_put` — upload a file to file-hub

Uploads a local file to file-hub, preserving the filename and content-type. Returns the new file-hub
UUID so the file can be retrieved later or shared.

**Use when:** you have a filled/processed document that needs to go back into file-hub for the
operator to download, sign, or forward.

**Example:** `file_hub_put(file_path="/data/file_hub_work/sepa_mandate_filled.pdf")`

### Typical workflow

1. `file_hub_get` — fetch the document from file-hub
1. `list_pdf_form_fields` — inspect what can be filled
1. `fill_pdf_document` — fill the form fields or overlay text
1. `file_hub_put` — upload the filled document back to file-hub

### Error handling

- **Unknown file id** — `file_hub_get` returns a clear "File not found" message.
- **File-hub unreachable** — all tools return a clear "File-hub unavailable" message with the
  connection error details.
- **Non-PDF input** — `fill_pdf_document` and `list_pdf_form_fields` return a clear "Not a valid
  PDF" message when the file lacks a `%PDF` header.
