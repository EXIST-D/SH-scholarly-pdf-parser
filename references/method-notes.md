# Method notes

## Pipeline (scripts/parse_pdf.py)
1. **Backend adapter** - PyMuPDF `get_text("dict")` lines with font size/bold/math-font flags, rotation, images and vector
   drawing clusters; pdfplumber and pypdf adapters produce the same line structure (pypdf has no geometry).
2. **Text-layer triage per page** - `text`, `scanned` (almost no characters and images/none), `garbled_text_layer`
   (U+FFFD / `(cid:)` > 5 %). Scanned/garbled pages go to OCR (tesseract TSV -> lines with bbox and confidence) within a
   time budget (`--ocr-budget`, default 420 s); otherwise they are listed in `unparsed`.
3. **Furniture** - lines in the top/bottom 9.5 % whose digit-normalised text repeats on >= 30 % of pages, plus bare page
   numbers, become `header`/`footer`; rotated text (arXiv stamp) becomes `margin`. Kept in the output, excluded from the body.
4. **Glyph repair** - Symbol-font private-use codes -> Greek/maths; ligatures expanded; `þ`->`+`, `¼`->`=` only if the
   document shows >= 2 unambiguous cases (logged).
5. **Tables** - `find_tables()`; cell text is rebuilt from character geometry (content-stream order scrambles decimals).
   If the grid is unreliable (wide gaps inside cells, stacked multi-line cells, caption text inside, 1-2 column blobs)
   the table is rebuilt from word positions: rows = baselines, columns = vertical white gaps, side-by-side tables are
   split when the halves repeat the same first header or the gap is very wide. Mostly-empty grids, > 60 rows and regions that
   contain running prose are rejected (vector diagrams, text boxes). Tables that continue on the next page are merged
   (same column count, bottom/top position, repeated header dropped).
6. **Figures** - images and vector-drawing clusters grown to include nearby short text; text inside is `figure_text`
   (kept in `figures[].inner_text`, removed from the body flow).
7. **Reading order** - blocks are ordered by recursive XY-cut: a full-height gutter splits columns; otherwise blocks that
   cross the dominant gutter (title, author rows, wide figures/captions) act as separators between bands, and each
   band is cut again. The gutter is found from a line-coverage histogram (<= 22 % of peak coverage), so author rows
   that overlap the gutter do not hide it.
8. **Line/paragraph rebuild** - same-baseline fragments are re-joined (justified text and font changes split PyMuPDF
   lines; bare section numbers / `[n]` markers are joined to their text); paragraphs split at first-line indents and style changes.
9. **Classification** - body font size = character-weighted mode; headings by size/bold/numbering/keywords with
   levels from numbering depth or size rank; captions need `Figure|Table N` + delimiter (so "Table 3 shows..." is prose);
   formulas by math-font ratio or equation-number pattern; footnotes by small size at page bottom; run-in headings flagged.
   Page-1 front matter: wrapped title lines merged, authors/affiliations -> `front_matter`, long pre-section paragraph -> `abstract`.
10. **Continuations** - a paragraph without terminal punctuation followed (in reading order) by a lower-case start on the
    next column/page is merged; soft hyphens are rejoined **unless** the hyphenated compound occurs elsewhere in the
    document (so `high-level` stays, `optimiza-tion` is rejoined). Every merge is recorded.
11. **References** - after a References heading; entries split by `[n]`, `n.` or hanging indent.

## Self-check (scripts/check_parse.py)
Independent plain extraction (ligatures expanded, line-end hyphens rejoined) vs parsed text as word multisets, per page;
plus duplicates, furniture leakage, same-column order inversions, garbled glyphs, heading-number gaps, ragged/sparse tables.

## Self-measured results (development samples, NOT platform scores)
Five public arXiv papers downloaded for development, Python 3.11, PyMuPDF 2.1, one core. Ground truth for order = paragraph
order of the LaTeX-derived ar5iv HTML (figures/tables excluded because they float). "Matched" = ground-truth paragraphs
found in the parse by 4-gram overlap (math is absent from the ground truth, so some paragraphs cannot match);
"order" = share of adjacent matched paragraph pairs in correct order; "recall" = check_parse word recall.

| paper | pages | layout | time | matched | order | recall |
|---|---|---|---|---|---|---|
| ResNet (1512.03385) | 12 | CVPR two-column, tables, figures | ~5 s | 96.1 % | 100 % | 99.7 % |
| BERT (1810.04805) | 16 | ACL two-column, booktabs tables | ~5 s | 92.9 % | 99.0 % | 98.7 % |
| Attention (1706.03762) | 15 | single column, equations | ~4 s | 98.4 % | 100 % | 99.8 % |
| GPT-3 (2005.14165) | 75 | single column, 40 tables | ~20 s | 97.5 % | 100 % | 99.9 % |
| LIGO (1602.03837) | 16 | PRL two-column, equations, `þ/¼` glyphs | ~5 s | no GT | no GT | 99.8 % |

Comparison with a naive baseline on the same ground truth (PyMuPDF `get_text("blocks")`):
- On these LaTeX-produced PDFs the default block order already follows reading order (baseline 96.6-100 %), while the
  popular `sort=True` is worse on two-column pages (ResNet 85.9 %, BERT 85.4 %). The Skill matches the best baseline there; its value is
  structure, table fidelity, evidence and checks, not raw order on this kind of file.
- **Synthetic stress test** (BERT pages 1-8 re-laid out with the same geometry but a shuffled content stream, as many
  Word/InDesign exports behave): Skill 98.2 % order vs baseline default 60.4 % and `sort=True` 83.0 %.
- Fallback backends on BERT: pdfplumber recall 97.8 % / order 96.9 %; pypdf is degraded by design (no geometry).

## Known limits (honest list)
- OCR: the integration (render -> tesseract TSV -> lines, bbox scaling, confidence flags, time budget) was exercised with a
  stub `tesseract` that emits fixed TSV; **no real OCR engine was available locally**, so OCR accuracy is untested.
- Not tested on real garbled-font (`(cid:)`) PDFs, CJK PDFs, rotated pages or three-column layouts; code paths exist.
- Formulas are linearised text from the text layer (no LaTeX recovery); stacked uncertainties (`+0.03 / -0.04`) can be detached.
- Borderless tables are rebuilt heuristically (medium confidence, flagged `strategy: words`); numeric columns with very
  narrow gaps can merge; merged-cell HTML only for ruled tables.
- Headings in documents with unusual typography (all-caps run-in headings, same font as body) may be missed.
- Table caption linking is nearest-caption; side-by-side tables can share one caption.
