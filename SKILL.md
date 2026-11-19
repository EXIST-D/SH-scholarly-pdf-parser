---
name: scholarly-pdf-parser
description: Parse a scientific/academic PDF (two-column papers, tables, equations, references, footnotes, scanned pages) into structured, evidence-linked output - reading-order text, section hierarchy, captions, tables, formulas, references - where every item points back to page and bounding box, with an independent self-check and honest uncertainty. Use whenever a task gives you a research PDF and asks for extraction, parsing, structuring, or answers that must cite the source page.
---

# scholarly-pdf-parser

Turns a research PDF into **verifiable structure**. One command parses the whole document; every block keeps
`page`, `bbox`, `type`, and a stable `id` (e.g. `p3-b012`), so any statement you make can be traced back to the page.
Nothing is guessed: unreadable regions are reported, never filled in.

## When to use / not use
- Use: input is a PDF paper/report; the task asks for text, sections, tables, formulas, references, captions,
  layout structure, or answers "with evidence/page numbers".
- Not needed: the input is already plain text/JSON/Markdown - read it directly (still cite source positions).
- This Skill does not interpret scientific meaning of figures/equations; they are located and extracted as structure.

## Quick start (3 commands, ~5 s for a 16-page paper)
`SKILL_DIR` = the directory containing this file (installed as `/skills/weekly-skill` by the evaluator).
```bash
python3 $SKILL_DIR/scripts/parse_pdf.py /input/<file>.pdf --out /app/work/parsed
python3 $SKILL_DIR/scripts/check_parse.py /input/<file>.pdf /app/work/parsed --out /app/work/check.json
python3 $SKILL_DIR/scripts/build_result.py --case /input/case.json --parsed /app/work/parsed \
        --check /app/work/check.json --out /app/output/result.json
```
Budget awareness (64 model requests, 900 s): run the three commands first, read the two summaries they print, then
spend the remaining effort only where `check_parse` reports a problem or where the task asks for specific content.

## Procedure
1. **Read the task.** Open `/input/case.json` and `task.md`. Note the exact `output_contract` fields and what the case
   asks for (full text? tables? sections? formulas? an answer with evidence?). Find the PDF(s) under `/input` (list the
   directory; do not assume file names). Never assume content from labels - read the file.
2. **Parse.** `parse_pdf.py INPUT.pdf --out DIR` writes `parsed.json` (all structure), `parsed.md` (readable, with
   `<!-- id pN -->` anchors) and `quality_report.json`. It prints one JSON line with `status`
   (`success | partial | failed`), counts, warnings.
   Options: `--ocr auto|off|force` (default auto: only pages without a text layer), `--ocr-engine auto|rapidocr|tesseract|pytesseract`,
   `--lang eng|chi_sim|eng+chi_sim` (tesseract only), `--backend auto|pymupdf|pdfplumber|pypdf`, `--max-pages N`,
   `--no-tables`, `--ocr-budget SECONDS` (default 420 for the whole document).
3. **Self-check.** `check_parse.py` compares the parse with an independent plain extraction (word recall per page),
   and tests duplicates, header/footer leakage, order inversions, garbled glyphs, heading-number gaps, ragged tables.
   It prints `verdict: pass | warn | fail` and `recommended_actions`. Act on them:
   - recall low -> re-run with `--backend pdfplumber`; garbled text layer -> `--ocr force`; keep the better parse.
   - order inversion -> open the listed block pairs (`locate_evidence.py --block ID`) and correct by reading `bbox`.
   - anything you cannot fix -> list it as uncertainty (step 6), never hide it.
4. **Read the result in chunks.** `parsed.md` is the fastest way to read; use `parsed.json` for tables
   (`tables[].rows`, `markdown`, `html` when cells are merged), `sections`, `formulas`, `references`, `figures`.
   Block types: `title, abstract, front_matter, heading(level, number), paragraph, list_item, caption, formula,
   footnote, table, reference_block, header, footer, margin`. Full schema: `references/output-schema.md`.
5. **Do what the case asks** using only what the parse contains. For every claim/answer keep its evidence:
   `python3 $SKILL_DIR/scripts/locate_evidence.py DIR --query "quoted text"` returns block id, page, bbox, section or
   `found:false` (then say it could not be located - do not invent a page).
6. **Write `/app/output/result.json`.** Put your decisions in `/app/work/answers.json` (field -> value) and run
   `build_result.py ... --answers /app/work/answers.json`. It fills `case_id`, `sources`, `status`, `uncertainty`,
   `change_records`, `tables`, `sections`, ... from the parse, takes everything else from your answers, leaves unknown
   fields `null` and lists them in `_meta.unfilled_fields`. Fill every listed field truthfully, then run
   `build_result.py --case /input/case.json --validate /app/output/result.json` (must print `"valid": true`).
   Also copy `parsed.json`, `parsed.md`, `quality_report.json`, `check.json` into `/app/output/` (editable artifacts).
7. **Save early.** After step 2 write a first `result.json` immediately (status `partial` if unsure) and improve it;
   a partial honest result beats a missing one.

## Status vocabulary (use in result.json `status`)
`success` all pages parsed, checks pass or only minor warnings | `partial` some pages/parts unreadable or unverified |
`undeterminable` the input cannot answer the question | `refused` encrypted/corrupt input that cannot be opened |
`failed` nothing usable. Always say which pages/regions are affected and why.

## Exception handling (details: `references/failure-modes.md`)
| Situation | What the tool does | What you do |
|---|---|---|
| Encrypted PDF | exits with `status:failed`, hint to ask for the password | do not guess passwords; report `refused` with reason |
| Not a PDF / corrupt / 0 pages | `status:failed` + error text | report, keep the message verbatim |
| Scanned page (no text layer) | OCR with `rapidocr` (pip, Chinese+English) or `tesseract` if installed; every OCR line carries `ocr_confidence`; else the page is marked `needs_ocr` in `unparsed` | if OCR was unavailable say so; do not transcribe from memory; treat OCR text with confidence < 60 as uncertain |
| Garbled fonts `(cid:..)`, U+FFFD, private-use glyphs | page kind `garbled_text_layer`, check -> fail | `--ocr force`, else report the pages as unreliable |
| Journal font exposes `+`/`=` as `þ`/`¼` | repaired only with document-level evidence, logged in warnings and change records | mention it as a change record |
| Tables without ruling lines | rebuilt from word positions (`strategy: words`) | treat cell alignment as medium confidence; say so |
| Image-only table / figure | extracted text only; figure inner text kept under `figures[].inner_text` | do not read numbers off images you cannot see |
| Very long PDF | `--max-pages N` to parse a prefix | report the limit as uncertainty |

## Dependencies (no network needed at run time)
Python 3.8+. Preferred: `pymupdf` (tested 2.1 / MuPDF 1.28; >=1.24 required for `find_tables` and
`cluster_drawings`). Fallbacks, chosen automatically: `pdfplumber` (tested 0.11), then `pypdf` (degraded: no geometry,
status `partial`). Optional OCR (only needed for scanned/garbled pages): `rapidocr` (pip; models are bundled in the wheel, no download at
run time; handles Chinese + English) - preferred; or `tesseract` CLI (+ language data) / `pytesseract`. Install if missing and
allowed: `pip install pymupdf pdfplumber pypdf rapidocr`. If no Python library is present, see the shell fallback in
`references/failure-modes.md` (poppler `pdftotext`/`pdftoppm`, `tesseract`) and report `partial`.

## Honesty rules (hard)
- Quote only text that exists in `parsed.json`; keep `text` verbatim and use `text_marked` only for sub/superscript.
- Never fabricate scientific data, pages, table cells, DOIs, or reference fields that are not in the PDF.
- A missing, unreadable or conflicting item is reported as such (`null` + uncertainty), not smoothed over.
- Describe changes you made (merged paragraphs, rebuilt tables, repaired glyphs, manual order fixes) in `change_records`.
- Do not claim scores, benchmark numbers or run logs; the platform records those.

## More
`README.md` (human guide + self-measured results), `references/method-notes.md` (how it works, thresholds, limits),
`examples/minimal-example.md` (commands + illustrative output, clearly marked as an example).
