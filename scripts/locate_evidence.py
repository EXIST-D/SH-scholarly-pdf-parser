#!/usr/bin/env python3
"""locate_evidence.py - map a quoted claim back to page / block / bbox in parsed.json.

usage:
  python3 locate_evidence.py PARSED_DIR_OR_JSON --query "text copied from the parse" [--top 3]
  python3 locate_evidence.py PARSED_DIR_OR_JSON --block p3-b012
  python3 locate_evidence.py PARSED_DIR_OR_JSON --table tbl2

Prints JSON. A hit is only reported when the match is real (score >= 0.55); otherwise `found: false`, so a claim that
cannot be located is never given an invented page number.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys


def toks(s):
    return re.findall(r"[a-z0-9À-ɏͰ-Ͽ一-鿿]+", (s or "").lower())


def load(p):
    pj = p if p.endswith(".json") else os.path.join(p, "parsed.json")
    return json.load(open(pj, encoding="utf-8"))


def score(q, text):
    qt, tt = toks(q), toks(text)
    if not qt or not tt:
        return 0.0
    if " ".join(qt) in " ".join(tt):
        return 1.0
    qs = set(qt)
    ov = len(qs & set(tt)) / len(qs)
    # best-window similarity for long blocks
    win = len(qt) + 4
    best = 0.0
    step = max(1, win // 2)
    for i in range(0, max(1, len(tt) - win + 1), step):
        best = max(best, difflib.SequenceMatcher(None, qt, tt[i:i + win]).ratio())
        if best > 0.95:
            break
    return round(0.5 * ov + 0.5 * best, 3)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("parsed")
    ap.add_argument("--query")
    ap.add_argument("--block")
    ap.add_argument("--table")
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args(argv)
    d = load(a.parsed)
    if a.block:
        for b in d["blocks"]:
            if b["id"] == a.block:
                print(json.dumps({"found": True, "block": b}, ensure_ascii=False, indent=1))
                return 0
        print(json.dumps({"found": False, "reason": "no such block id"}))
        return 1
    if a.table:
        for t in d["tables"]:
            if t["id"] == a.table:
                print(json.dumps({"found": True, "table": t}, ensure_ascii=False, indent=1))
                return 0
        print(json.dumps({"found": False, "reason": "no such table id"}))
        return 1
    if not a.query:
        ap.error("need --query, --block or --table")
    cands = []
    for b in d["blocks"]:
        if b["type"] in ("header", "footer", "margin"):
            continue
        s = score(a.query, b.get("text", ""))
        if s:
            cands.append((s, "block", b))
    for t in d["tables"]:
        s = score(a.query, " ".join(" ".join(c or "" for c in r) for r in t["rows"]))
        if s:
            cands.append((s, "table", t))
    for r in d.get("references", []):
        s = score(a.query, r["text"])
        if s:
            cands.append((s, "reference", r))
    cands.sort(key=lambda x: -x[0])
    hits = []
    for s, kind, o in cands[: a.top]:
        if s < 0.55:
            continue
        h = {"score": s, "kind": kind, "page": o.get("page") or o.get("pages", [None])[0], "bbox": o.get("bbox")}
        if kind == "block":
            h.update({"block_id": o["id"], "type": o["type"], "pages": o.get("pages", [o["page"]]),
                      "section": o.get("section"), "snippet": o["text"][:200]})
        elif kind == "table":
            h.update({"table_id": o["id"], "pages": o["pages"], "snippet": o["markdown"][:200]})
        else:
            h.update({"reference_id": o["id"], "snippet": o["text"][:200]})
        hits.append(h)
    print(json.dumps({"found": bool(hits), "query": a.query[:120], "hits": hits}, ensure_ascii=False, indent=1))
    return 0 if hits else 1


if __name__ == "__main__":
    sys.exit(main())
