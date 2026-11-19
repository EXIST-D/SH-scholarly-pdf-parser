#!/usr/bin/env python3
"""Dev tool: build a SYNTHETIC test PDF with the same visual layout as pages of a real paper but a shuffled
content-stream order (what Word/InDesign exports often look like). Used only to stress-test reading-order logic."""
import random, sys
import pymupdf
src, dst, first, last = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
d = pymupdf.open(src)
out = pymupdf.open()
rnd = random.Random(7)
for pno in range(first - 1, last):
    p = d[pno]
    new = out.new_page(width=p.rect.width, height=p.rect.height)
    items = []
    for b in p.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        txt = "\n".join("".join(s["text"] for s in l["spans"]) for l in b["lines"])
        size = max((s["size"] for l in b["lines"] for s in l["spans"]), default=9)
        if txt.strip():
            items.append((b["bbox"], txt, size))
    rnd.shuffle(items)
    for bbox, txt, size in items:
        rect = pymupdf.Rect(bbox[0], bbox[1] - 1, bbox[2] + 6, bbox[3] + 3)
        fs = size * 0.95
        while fs > 4 and new.insert_textbox(rect, txt, fontsize=fs, fontname="tiro") < 0:
            fs -= 0.4
out.save(dst)
print("saved", dst, len(out), "pages")
