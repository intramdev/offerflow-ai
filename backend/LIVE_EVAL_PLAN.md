# Live Meta evaluation plan — conversational import

**Status: NOT RUN.** Nothing below has been executed against a real
provider. The mocked suites verify contracts and failure paths; they
say nothing about model accuracy. Do not cite this plan as evidence
until the run below is authorized, executed, and its results table is
filled in.

## 1. Goal and non-goals

Measure whether the Meta lane turns unfamiliar supplier layouts into
correct, evidence-linked drafts through the real
propose → apply → import → export slice. Out of scope for this run:
quote matching, categorization, the help-assistant chat, and any
model-vs-model comparison.

## 2. Dataset (fixed synthetic inputs, no private content)

All fixtures are synthetic price lists with invented prices. No real
supplier files, chat backups, or customer data take part. Fixture S4
appears only in this plan — it is in no prompt, template, or test.

### S1 — sectioned message, 33 products (contract case A)

Four grade sections with 4/8/13/8 products. Gold: exactly 33 records;
headers and logistics/date lines are not products; terminal amounts
are USD prices; quantities stay unknown (price-only).

```text
Meridian Traders - November list
Ready in DXB and SIN
Updated 02 Nov
P1+ Like New
Pear Phone 18 Pro 256GB - 417
Pear Phone 18 128GB - 690
Pear Phone 17 128GB - 505
Pear Phone 16 128GB - 195
P2+ Grade A Tested
Pear Phone 18 Pro 128GB - 399
Pear Phone 17 Pro 128GB - 610
Pear Phone 17 128GB - 470
Pear Phone 16 128GB - 210
Pear Phone 15 256GB - 260
Pear Phone 15 128GB - 240
Pear Phone 14 128GB - 190
Pear Tab A9 64GB - 150
P3+ Grade A- Tested
Pear Phone 18 Pro 128GB - 370
Pear Phone 17 Pro 128GB - 560
Pear Phone 17 128GB - 430
Pear Phone 16 Pro Max 128GB - 520
Pear Phone 16 128GB - 195
Pear Phone SE 64GB - 120
Pear Phone 15 Pro Max 128GB - 480
Pear Phone 15 256GB - 235
Pear Phone 15 128GB - 215
Pear Phone 15 Mini 128GB - 200
Pear Phone 14 64GB - 150
Pear Phone 14 Mini 128GB - 170
Pear Tab S9 128GB - 420
P4+ Grade B/C
Pear Phone 17 Plus 128GB - 400
Pear Phone 17 128GB - 390
Pear Phone 16 128GB - 175
Pear Phone 15 Pro Max 128GB - 430
Pear Phone 15 256GB - 210
Pear Phone 15 128GB - 195
Pear Phone 14 Mini 128GB - 150
Pear Phone 14 Mini 64GB - 140
End of list - prices in USD
```

Expected ambiguity: the model must ask (or correctly infer with
evidence) that the trailing number is the USD price and that no
quantities are given. Scripted clarification: "The last number on
each product line is the USD price. No quantities are listed."

### S2 — row-ten spreadsheet, two sheets (contract case K)

Sheet `Gadgets`: 9 banner rows, then
`supplier, brand, model, capacity, color, grade, lock, quantity,
price, currency, conditions`, then 2 data rows. Sheet `Extras`: the
same headers, then 2 data rows. Gold: 4 products total; selecting
`Gadgets` discloses `Extras` as omitted (and vice versa); no silent
loss. Build with the `make_multi_workbook` pattern from
`pipeline/tests/test_parsing.py`. No clarification is expected; any
model call here would be a routing failure.

### S3 — inline and multi-line layouts (contract case G)

Reuse `INLINE_TEXT` (inline `Model: … | qty … | price` rows, tablets
and a charger, 3 products) and `MULTILINE_TEXT` (two 3-line joined
products) from `pipeline/tests/test_generalization.py`. Gold: 3 + 2
records with prices 189/799/19 and 417/505. Scripted clarification
for the join case: "Lines 2-4 are one product and lines 5-7 are
another; the price is on the last line of each."

### S4 — held-out bracketed layout (seen nowhere else)

```text
Sterling Mobile - 04 Nov
[SM-A156] Pear A16 128GB Graphite || 12 pcs @ 188
[SM-S921] Pear S25 256GB Silver || 6 pcs @ 640
[SM-X210] Tab A9 64GB Graphite || 20 pcs @ 175
Prices include VAT. Delivery DXB 3 days.
```

Gold: 3 records, prices 188/640/175 USD-unknown (currency is NOT
stated — the model must ask or leave it unknown, never assume USD);
quantities accepted only if grounded on the `pcs` markings, else a
review question; the VAT/delivery line is a note, not a product.
Scripted clarification: "The number after @ is the price. The
currency is not stated."

## 3. Protocol (per scenario)

1. **Raw pass.** Paste/upload, then send one fixed opener: "What do
   you see in this document?" Record the first proposal: coverage,
   per-field values, and whether it asked the expected question.
   Apply nothing yet.
2. **Clarified pass.** Send the scripted clarification, review the
   proposal cards, apply, and record the corrected preview.
   Refresh the page and confirm the preview is unchanged
   (persistence check).
3. **Import.** Confirm the exact revision, answer open reviews with
   the gold values, approve, and export XLSX plus the message text.
   Compare preview, exported file, and message field by field.

## 4. Metrics (report every denominator)

| # | Metric | Denominator |
|---|--------|-------------|
| 1 | Product coverage (raw / clarified) | 33, 4, 5, 3 records |
| 2 | Field accuracy: model, capacity, grade, price, currency, quantity | proposed cells per field |
| 3 | Invented critical values (price/qty/capacity/grade/currency) | must be 0 in all 4 |
| 4 | Expected question asked (raw pass) | 3 scenarios (not S2) |
| 5 | Correction applied to preview AND import | 3 scenarios |
| 6 | Preview stable across refresh | 4 scenarios |
| 7 | Preview = export = message | 4 scenarios |
| 8 | Requests, input/output tokens, latency, costed usage | from the usage log |

A tiny fixture set is not broad reliability evidence: report the
sample size (4 layouts, ≤44 products) alongside every rate.

## 5. Budget and hard stops

- Provider: Meta API chat completions, model id recorded at run time
  (default `muse-spark-1.3-contributor` unless the owner picks
  otherwise). Contributor-tier inputs train Meta's models; only the
  synthetic fixtures above are sent.
- Per-request caps (app settings): 8,000 input + 2,000 output
  tokens; 1 in flight; retries off.
- Run ceiling: **24 requests** (operator-tallied; 4 scenarios × ≤5
  calls + margin) and **240,000 tokens** (app-enforced org
  allowance). Expected real use is under 30,000 tokens (<$0.01 at
  Contributor list prices $0.10/M in, $0.20/M out); the 240k ceiling
  is the cannot-exceed bound, about $0.05 at list output rates.
- Stop immediately on: any invented price/quantity, any
  cross-company data in a reply, 3 consecutive unusable proposals,
  80% of either ceiling (warn) / 100% (halt), or any 500/error that
  blocks honest accounting. Pausing never unspends accepted calls.

## 6. Setup and authorization checklist

- [ ] Owner approves provider, model id, and the 24-request /
      240k-token ceiling; date and approver recorded here.
- [ ] Fresh eval organization with its allowance set to 240,000
      tokens; operator ceiling covers it; kill switches verified.
- [ ] Worker started with the integration overlay; simulated stack
      untouched otherwise.
- [ ] Each scenario run in order; results table below filled the
      same day; usage log exported with the report.

## 7. Results (fill during the authorized run)

| Scenario | Coverage raw → clarified | Field errors | Invented | Asked? | Persist? | Preview=export? | Req/tok |
|----------|--------------------------|--------------|----------|--------|----------|-----------------|---------|
| S1 (33)  | / | | | | | | |
| S2 (4)   | / | | | n/a | | | |
| S3 (5)   | / | | | | | | |
| S4 (3)   | / | | | | | | |

Model id: ________ Approved by: ________ Date: ________
