# dev tools (not part of the Skill)

Helpers used while developing the Skill, kept for reproducibility. They need `pymupdf` and public sample PDFs
(arXiv) plus their ar5iv HTML versions as ground truth.

- `eval_order.py parsed.json gt.html` - paragraph-order accuracy against a LaTeX-derived (ar5iv) HTML page.
- `baseline_eval.py paper.pdf gt.html tag` - same metric for a naive PyMuPDF `get_text("blocks")` baseline.
- `make_shuffled.py in.pdf out.pdf first last` - builds a SYNTHETIC PDF with the same geometry but a shuffled content stream.
