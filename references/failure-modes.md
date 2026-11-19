# Failure modes, diagnosis and handling

| Symptom (where you see it) | Likely cause | Action |
|---|---|---|
| `status: failed`, "encrypted PDF" | password-protected | Report `refused`; ask for the password if the task allows; never guess |
| `status: failed`, "not a PDF" / "no readable pages" | wrong file, truncated or corrupt download | Report verbatim; if another copy exists under `/input`, parse that |
| `unparsed[].reason == needs_ocr` | page has no text layer (scan) and no OCR engine (install `rapidocr`), or OCR budget used up | State OCR was unavailable/limited; list pages; do not transcribe from memory |
| OCR text looks wrong (`ocr_confidence` < 60, `low_ocr_confidence` flag) | low resolution, skew, noise, handwriting | Report those blocks as uncertain; never "correct" them from memory |
| arXiv-style rotated watermark appears as junk headings/margin fragments on OCR pages | rotated text recognised as horizontal fragments | Ignore blocks of type `margin`; discard junk fragments and log it |
| page kind `garbled_text_layer`, check `garbled` | broken font encoding: `(cid:NN)`, U+FFFD, private-use glyphs | `--ocr force` (needs an OCR engine, e.g. `pip install rapidocr`) else report pages as unreliable |
| check `recall` < 93 % on a page | text inside figures/tables, hidden layers, or parser loss | Compare with `--backend pdfplumber`; read `missing_sample` words; keep best parse; log the rest as uncertainty |
| check `order_inversion` | unusual layout (side bars, text boxes, rotated blocks) | Read `bbox` of the pair, fix order by geometry, log in change records |
| check `furniture_leak` | running head not recognised (appears on <30 % of pages) | Remove the string from the block, log it |
| numbers/decimals look split (`80 . 6`) | text-layer glyph order | Tables: the parser re-orders by geometry; for body text re-run with another backend and compare |
| `+` shown as `þ`, `=` as `¼` | journal font with Latin-1 operator codes | Auto-repaired only with document evidence (logged); verify visually if critical |
| table cells merged in one string | borderless table, `strategy: words` fallback failed | Re-read the table region via `locate_evidence.py --table ID`; report as medium confidence |
| side-by-side tables merged/split wrongly | shared rules / wide gaps | Check `tables[].caption` and `bbox`; mention the ambiguity |
| stacked +/- uncertainties detached (`z = 0.09` ... `+0.03 -0.04` as separate lines) | sub/superscript stacks emitted as separate text lines | Reattach manually using `bbox`, log it |
| CJK / non-Latin text | works through the text layer; rapidocr OCR covers Chinese+English out of the box | with tesseract use `--lang chi_sim` (or `eng+chi_sim`) |
| pypdf backend used | no PyMuPDF/pdfplumber installed | Status is `partial`: no geometry, order/columns/tables not verified |

## Shell fallback when no Python PDF library exists (not exercised locally - report `partial`)
```bash
pdftotext -layout -enc UTF-8 in.pdf out.txt        # poppler; page breaks are form-feed characters
pdftoppm -r 300 -png in.pdf /app/work/page         # render pages for OCR
tesseract /app/work/page-1.png /app/work/page-1 -l eng --psm 1 tsv
```
Keep page numbers from the form feeds / file names, and mark every result as unverified for reading order and tables.

## Reporting template for a failed or partial run
`status`, `pages_ok`, `pages_failed` with reason per page, `what_was_tried` (commands + flags), `what_the_user_can_do`
(provide password / better scan / install tesseract). Keep it short and factual.
