#!/usr/bin/env python3
"""Local self-check: compare our block order against the paragraph order of an ar5iv (LaTeX-derived) HTML version.

This is a development tool for MY OWN samples (public arXiv papers). It is not part of the submitted Skill.
usage: eval_order.py parsed.json ground_truth.html
"""
import json
import re
import sys
from html.parser import HTMLParser

KEEP_CLASSES = ("ltx_p", "ltx_title", "ltx_caption", "ltx_abstract", "ltx_para")


class GT(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.paras, self.cur = [], [], None
        self.skip = 0

    SKIP = {"ltx_bibliography", "ltx_authors", "ltx_tabular", "ltx_equation", "ltx_equationgroup", "ltx_note", "ltx_dates", "ltx_role_footnote"}

    def is_skip(self, cls):
        return bool(self.SKIP & set(cls.split()))

    VOID = {"br", "img", "hr", "meta", "link", "input", "source", "col", "area", "base", "wbr"}

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        cls = dict(attrs).get("class", "") or ""
        self.stack.append((tag, cls))
        if tag in ("math", "table", "script", "style", "annotation") or self.is_skip(cls):
            self.skip += 1
        if self.skip == 0 and (tag == "p" and "ltx_p" in cls.split() or "ltx_title" in cls.split() or tag == "figcaption"):
            self.cur = []

    def handle_endtag(self, tag):
        if not self.stack or tag in self.VOID:
            return
        t, cls = self.stack.pop()
        if t in ("math", "table", "script", "style", "annotation") or self.is_skip(cls):
            self.skip -= 1
        if self.cur is not None and self.skip == 0 and (t == "p" and "ltx_p" in cls.split() or "ltx_title" in cls.split() or t == "figcaption"):
            txt = " ".join("".join(self.cur).split())
            if len(txt) > 3:
                self.paras.append(txt)
            self.cur = None

    def handle_data(self, data):
        if self.cur is not None and self.skip == 0:
            self.cur.append(data)


def toks(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def main(pj, gh):
    d = json.load(open(pj))
    g = GT()
    g.feed(open(gh, encoding="utf-8", errors="ignore").read())
    # figures/tables float: their position in the LaTeX-derived HTML is not their position on the PDF page
    gt = [toks(p) for p in g.paras if len(toks(p)) >= 8 and not re.match(r"^\s*(figure|table|fig)\b", p.strip().lower())]
    ours = []
    for b in d["blocks"]:
        if b["type"] in ("title", "heading", "paragraph", "caption", "list_item", "front_matter", "footnote", "formula"):
            t = toks(b["text"])
            if len(t) >= 3:
                ours.append((b["id"], b["type"], b["page"], t))
    # map each GT paragraph to the best matching block via 6-gram overlap of its first 24 tokens
    def grams(t, n=4):
        return {tuple(t[i:i + n]) for i in range(max(0, len(t) - n + 1))}
    og = [grams(t) for _, _, _, t in ours]
    idx, found = [], 0
    for p in gt:
        pg = grams(p[:60])
        if not pg:
            idx.append(None)
            continue
        best, bs = None, 0
        for k, o in enumerate(og):
            if not o:
                continue
            s = len(pg & o) / len(pg)
            if s > bs:
                best, bs = k, s
        if bs >= 0.5:
            idx.append(best)
            found += 1
        else:
            idx.append(None)
    pairs = ok = 0
    prev = None
    for k in idx:
        if k is None:
            continue
        if prev is not None:
            pairs += 1
            ok += 1 if k >= prev else 0
        prev = k
    print("GT paragraphs: %d | matched: %d (%.1f%%)" % (len(gt), found, 100.0 * found / max(1, len(gt))))
    print("adjacent-pair order accuracy: %d/%d = %.1f%%" % (ok, pairs, 100.0 * ok / max(1, pairs)))
    bad = []
    prev = None
    for n, k in enumerate(idx):
        if k is None:
            continue
        if prev is not None and k < prev[1]:
            bad.append((prev[0], n, " ".join(gt[prev[0]][:8]), " ".join(gt[n][:8])))
        prev = (n, k)
    for b in bad[:12]:
        print("  order break GT#%d->#%d: %r -> %r" % b)
    miss = [" ".join(gt[n][:10]) for n, k in enumerate(idx) if k is None]
    print("unmatched GT paragraphs (first 8):")
    for m in miss[:8]:
        print("   -", m)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
