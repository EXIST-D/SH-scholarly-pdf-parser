#!/usr/bin/env python3
"""Dev tool: a naive baseline (PyMuPDF get_text blocks, optionally sort=True) scored with the same ground-truth
paragraph-order metric as eval_order.py, to quantify what the Skill adds. Not part of the submitted Skill."""
import json, sys, subprocess, os
import pymupdf

def baseline(pdf, out_json, sort):
    d = pymupdf.open(pdf)
    blocks = []
    for pno, page in enumerate(d):
        for b in page.get_text("blocks", sort=sort):
            if b[6] == 0 and b[4].strip():
                blocks.append({"id": "b%d" % len(blocks), "type": "paragraph", "page": pno + 1, "text": " ".join(b[4].split())})
    json.dump({"blocks": blocks}, open(out_json, "w"))

if __name__ == "__main__":
    pdf, gt, tag = sys.argv[1:4]
    for sort in (False, True):
        out = "edge/baseline_%s_%s.json" % (tag, "sorted" if sort else "raw")
        baseline(pdf, out, sort)
        r = subprocess.run([sys.executable, "-I", "eval_order.py", out, gt], capture_output=True, text=True).stdout.split("\n")[:2]
        print("[%s %s]" % (tag, "sort=True" if sort else "default"), " | ".join(r))
