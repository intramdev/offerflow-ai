# OfferFlow AI example datasets

Synthetic inputs for reproducing manual import checks. All supplier names in these fixtures are fictional. Product names may refer to real manufacturers; these are not commercial offers.

Start with `inputs/manual/06-headerless-semicolon.txt` or `04-cedar-sections.txt`.
Use a local test workspace. Real assistant requests can consume provider tokens.
Do not upload the whole directory as one supplier document.

## Folders

- `inputs/manual/`: original small inputs recovered from actual manual acceptance runs.
- `inputs/header-detection/`: synthetic CSV fixtures with headers on rows 1, 4 and 10.
- `inputs/generalization/`: additional synthetic layouts; not a certified benchmark.
- `inputs/nordo/`: the original synthetic office-supply price list, in equivalent XLSX and CSV formats. Import one version, not both.
- `reference/`: expected values and reviewer-only checks. Do not provide this folder to the model during evaluation.
- `generators/`: scripts for creating large upload fixtures locally; large files are not tracked.
- `TEST_CASES.csv`: purpose, evidence and limitations for each case.
- `manifest.json` and `SHA256SUMS.txt`: inventory, sizes and integrity hashes.

## Quick checks

### Headerless offers
Paste `06-headerless-semicolon.txt`. The supplier is **UI Split Acceptance**, supplied by the test operator rather than present in the text.
Ask the assistant to explain the field positions, then confirm:
model; capacity; grade; unit price; stock quantity.
Prices are EUR 173 and EUR 379. Stock quantities are 9 and 6.
Review and apply the proposed split. Both rows must survive, including the first one.
After approval, compare the exported rows against `reference/manual_expected.json`.

### Sectioned price-only offers
Paste `04-cedar-sections.txt`.
If asked, confirm that final numbers are USD unit prices and stock quantities are not supplied.
Use the price-only policy. Preserve each grade section and keep quantity unknown.
Metadata, dates and headings must not become product rows.

### Office products and conflicting prices
Upload `05-office-price-conflict.csv`.
Choose “Other products” when asked whether capacity and grade apply.
The two organiser rows disagree on price. Neither is automatically authoritative.
For a reproducible **synthetic operator decision**, choose EUR 13.50: retain the EUR 12 row as excluded evidence, without counting its stock again.
This chosen value is a test decision, not an inference that a higher price is more current.

### NORDO stress test
Upload only `inputs/nordo/price_list.xlsx` or its CSV equivalent.
Supply the fictional supplier name NORDO Office Supply.
There are 24 source records, 23 distinct SKUs and conflicting prices for NDO-007.
The complete source scenario also includes PDF/images and cross-source reconciliation. Those capabilities are not established by importing this spreadsheet.
The original large PDF and image collection are intentionally omitted from this lightweight repository package. They were not successfully imported in the reviewed app.
The NORDO reference files describe expected scenario behavior, not a claim that OfferFlow implements every feature.
Do not invent missing dimensions, approve automatically, or treat purchase costs as selling prices.

## Large uploads

Run `python3 generators/make_upload_fixtures.py --output-dir /tmp/offerflow-large-fixtures`.
This creates 12,000,000-byte, exactly 200,000,000-byte, and 200,000,001-byte CSV test files locally.
The valid files contain 1999 data rows plus a header and intentionally large synthetic Notes cells.
Do not confirm these large previews into a model-backed import just to test upload intake.
Independent browser checks found 12 MB previews and over-limit refusal working. The exactly-200 MB preview initially failed on 110bdc0; after correction 982f222, a fresh browser upload and refresh/resume both passed. This verifies upload and preview, not a model-backed import of all large-file rows. A direct upload success alone is not proof of successful preview or parsing. See TEST_CASES.csv.
Keep generated files out of Git.

## Scope and data provenance

This package covers selected manual acceptance inputs and supplemental fixtures. Automated test fixtures remain in the repository test suite; this is not an archive of every test execution.
No real WhatsApp exports, contacts, supplier offers, API keys, session cookies, database backups, screenshots or internal infrastructure settings are included.
The small fixture set is not a representative accuracy benchmark or a fine-tuning dataset.
Publishing expected answers makes these examples public regression cases, not a hidden evaluation set.
Keep fresh, separate held-out layouts for measuring generalization.
Use the hosting repository’s license for this package; no separate license is introduced here.
