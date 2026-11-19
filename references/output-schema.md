# parsed.json schema (`scholarly-pdf-parser/1`)

```
{
  "schema": "scholarly-pdf-parser/1", "tool_version": "1.0.0",
  "source":   {"file", "sha256", "pages", "backend", "metadata", "body_font_size"},
  "status":   "success | partial | failed",
  "pages":    [{"page", "width", "height", "kind", "columns", "ocr", "ocr_note", "n_lines", "n_figures", "n_tables"}],
  "blocks":   [ Block, ... ]            // reading order = array order = Block.order
  "sections": [{"id", "title", "number", "level", "block", "page"}],
  "tables":   [ Table, ... ],  "formulas": [ Formula, ... ],  "references": [ Reference, ... ],
  "figures":  [{"id", "page", "bbox", "caption", "inner_text"}],
  "furniture":{"headers": [{"page","text"}], "footers": [...], "margins": [...]},
  "unparsed": [{"page", "reason", "detail"}],      // e.g. needs_ocr, extraction_error
  "warnings": [str], "stats": {...}
}
```
Coordinates are PDF points, origin top-left, `[x0, y0, x1, y1]`. Pages are 1-based.

## Block
| field | meaning |
|---|---|
| `id` | stable id `p<page>-b<seq>`; use it as evidence |
| `type` | `title, abstract, front_matter, heading, paragraph, list_item, caption, formula, footnote, table, reference_block, header, footer, margin` |
| `page`, `pages` | start page; `pages` lists all pages when a paragraph continues across a column/page break |
| `bbox`, `column` | box on the start page; `column` is a coarse label (`full`, 0, 1...) - use `bbox` for geometry |
| `order` | global reading order |
| `text` | verbatim text (ligatures expanded, line-end soft hyphens rejoined) |
| `text_marked` | same text with `^{sup}` / `_{sub}` markup, only when the line has sub/superscripts |
| `level`, `number` | headings: level (1 = top) and printed number (`3.2`, `A.1`) |
| `section` | id of the section the block belongs to |
| `flags` | e.g. `dehyphenated:N`, `run_in_heading:<phrase>`, `low_ocr_confidence`, `caption_no_delimiter` |
| `continuation` | for merged paragraphs: bboxes of the continued parts (page, bbox, block id) |
| `ocr_confidence` | 0-100, only for OCR'd text |
| `table_id` | for `type: table`, id into `tables[]` |

## Table
`{"id","block","pages","bbox","n_rows","n_cols","rows":[[str]],"markdown","html"?,"strategy":"lines|words","caption":{"block","text"}|null,"merged_from":[ids],"flags":[...],"confidence","note"?}`
- `strategy: lines` = ruled grid from the PDF; `words` = rebuilt from word positions (borderless tables, medium confidence).
- `html` is present when merged (row/col-span) cells were detected; use it instead of `markdown` then.
- `flags`: `has_merged_cells`, `merged_across_pages[_repeated_header_dropped]`.

## Formula / Reference
`Formula {"id","block","page","bbox","number","text","text_marked","note"}` - linearised from the text layer (not LaTeX).
`Reference {"id","label","text","page","bbox","block"}` - entries split by `[n]`, `n.` or hanging indent.

## quality_report.json
`{"status","backend","pages","stats","warnings","unparsed","page_kinds","columns_per_page"}`

## check.json (from check_parse.py)
`{"summary": {"verdict","mean_token_recall","min_token_recall","recommended_actions",...}, "issues": [...], "per_page_recall": [...]}`
