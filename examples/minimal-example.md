# Minimal example

> **EXAMPLE - illustrative only.** Everything below was produced by running this Skill on a public arXiv paper
> (BERT, arXiv:1810.04805, a file I downloaded for development). It is **not** an official case, not a score,
> and not a platform run log. The real inputs arrive as `/input/case.json` plus the PDF.

## 1. Parse
```bash
python3 scripts/parse_pdf.py paper.pdf --out parsed
```
stdout (one line):
```json
{"status": "success", "out": "parsed", "pages": 16, "stats": {"blocks": 346, "paragraphs_merged_across_column_or_page": 5, "headings": 37, "tables": 8, "formulas": 1, "references": 142, "figures": 5}, "warnings": 0, "unparsed": 0}
```

## 2. Self-check
```bash
python3 scripts/check_parse.py paper.pdf parsed --out check.json
```
```json
{"verdict": "warn", "independent_extractor": "pymupdf", "mean_token_recall": 0.9873, "min_token_recall": 0.9065, "pages": 16, "blocks": 346, "tables": 8, "issue_count": 2, "recommended_actions": ["Low recall: re-run with `--backend pdfplumber`, then compare; list the missing words as uncertainty if they cannot be recovered.", "Order inversions: open the listed block pairs in parsed.json (bbox) and correct the order manually; state it in change records."]}
```
`recommended_actions` lists what to do about each finding (here: low recall on one figure-heavy page, one order inversion
to inspect). Nothing is hidden: unresolved items go to `uncertainty` in result.json.

## 3. What the structure looks like
Headings (`type`, page, level, text):
```
  p1 L0 BERT: Pre-training of Deep Bidirectional Transformers for La
  p1 L2 Abstract
  p1 L1 1 Introduction
  p2 L1 2 Related Work
  p2 L2 2.1 Unsupervised Feature-based Approaches
  p2 L2 2.2 Unsupervised Fine-tuning Approaches
  p3 L2 2.3 Transfer Learning from Supervised Data
  p3 L1 3 BERT
  ...
```
A table (`tables[0]`, strategy `lines`, pages [6]), decimals intact:
```
| System | MNLI-(m/mm) | QQP | QNLI | SST-2 | CoLA | STS-B | MRPC | RTE | Average |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  | 392k | 363k | 108k | 67k | 8.5k | 5.7k | 3.5k | 2.5k | - |
| Pre-OpenAI SOTA | 80.6/80.1 | 66.1 | 82.3 | 93.2 | 35.0 | 81.0 | 86.0 | 61.7 | 74.0 |
| BiLSTM+ELMo+Attn | 76.4/76.1 | 64.8 | 79.8 | 90.4 | 36.0 | 73.3 | 84.9 | 56.8 | 71.0 |
```

## 4. Evidence lookup
```bash
python3 scripts/locate_evidence.py parsed --query "BERT alleviates the previously mentioned unidirectionality constraint" --top 1
```
```json
{"score": 1.0, "block_id": "p1-b016", "type": "paragraph", "pages": [1, 2], "page": 1, "bbox": [307.3, 659.7, 525.5, 765.6]}
```
A quote that is not in the document returns `{"found": false, ...}` - never an invented page.

## 5. Result file
`examples/case.example.json` is a made-up contract (the real one is `/input/case.json`).
```bash
python3 scripts/build_result.py --case examples/case.example.json --parsed parsed --check check.json \
        --answers answers.json --out result.json
python3 scripts/build_result.py --case examples/case.example.json --validate result.json   # -> {"valid": true, "problems": []}
```
`result.json` then holds `case_id`, `status`, `sections`, `tables`, `references`, `sources` (file + sha256 + pages +
backend), `uncertainty` (warnings, unparsed pages, failed checks), `change_records` (merged paragraphs, rebuilt tables,
rejoined hyphens, glyph repairs) and whatever you put in `answers.json` (e.g. `evidence`).
