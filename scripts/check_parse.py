#!/usr/bin/env python3
"""check_parse.py - self-check a parse produced by parse_pdf.py against an INDEPENDENT text extraction of the same PDF.

usage: python3 check_parse.py INPUT.pdf PARSED_DIR_OR_JSON [--out report.json]

What it measures (all computed from the actual files, nothing is assumed):
  * token recall per page      - words present in a plain text extraction but missing from the parsed output
  * duplicated paragraphs      - the same long paragraph emitted twice
  * furniture leakage          - running headers / page numbers that ended up inside body text
  * order inversions           - a block placed above its predecessor inside the same column
  * garbled text layer         - U+FFFD, (cid:NN), private-use glyphs  (=> OCR or another backend is needed)
  * numbered-heading gaps      - e.g. sections 1,2,4 (3 missing)
  * ragged / empty tables
It prints a verdict (pass | warn | fail) and recommended_actions. It never edits the parse.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter


LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\u00ad": ""}


def norm_ref(txt):
    """Normalise the independent extraction the same way a reader would: ligatures expanded, line-end hyphens rejoined."""
    for k, v in LIG.items():
        txt = txt.replace(k, v)
    return re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", txt)


def toks(s):
    return re.findall(r"[A-Za-z0-9À-ɏͰ-Ͽ一-鿿]{2,}", (s or "").lower())


def plain_pages(pdf):
    """Independent extraction: PyMuPDF plain text, else pypdf. Returns (pages, backend_name) or (None, reason)."""
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except Exception:
            import fitz  # type: ignore
        doc = fitz.open(pdf)
        if doc.needs_pass:
            doc.authenticate("")
        return [p.get_text("text") for p in doc], "pymupdf"
    except Exception as e1:
        try:
            try:
                from pypdf import PdfReader  # type: ignore
            except Exception:
                from PyPDF2 import PdfReader  # type: ignore
            r = PdfReader(pdf)
            return [(p.extract_text() or "") for p in r.pages], "pypdf"
        except Exception as e2:
            return None, "no independent extractor (%s | %s)" % (str(e1)[:60], str(e2)[:60])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("parsed")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    pj = a.parsed if a.parsed.endswith(".json") else os.path.join(a.parsed, "parsed.json")
    doc = json.load(open(pj, encoding="utf-8"))
    blocks = doc["blocks"]
    actions, issues = [], []

    # ---- 1. token recall per page vs independent extraction
    pages_plain, backend = plain_pages(a.pdf)
    per_page = []
    if pages_plain is None:
        issues.append({"check": "recall", "severity": "info", "detail": backend})
    else:
        parsed_tokens = {}
        for b in blocks:
            txt = b.get("text", "")
            parsed_tokens.setdefault(b["page"], Counter()).update(toks(txt))
            for p in b.get("pages", [])[1:]:
                parsed_tokens.setdefault(p, Counter()).update(toks(txt))
        for t in doc.get("tables", []):
            for pg in t["pages"]:
                for r in t["rows"]:
                    parsed_tokens.setdefault(pg, Counter()).update(toks(" ".join(c or "" for c in r)))
        for f in doc.get("figures", []):
            parsed_tokens.setdefault(f["page"], Counter()).update(toks(" ".join(f.get("inner_text", []))))
        for i, txt in enumerate(pages_plain):
            ref = Counter(toks(norm_ref(txt)))
            tot = sum(ref.values())
            got = parsed_tokens.get(i + 1, Counter())
            miss = ref - got
            nmiss = sum(miss.values())
            rec = 1.0 - (nmiss / tot) if tot else 1.0
            per_page.append({"page": i + 1, "tokens": tot, "recall": round(rec, 4),
                             "missing_sample": [w for w, _ in miss.most_common(8)]})
            if tot >= 30 and rec < 0.93:
                issues.append({"check": "recall", "severity": "warn" if rec >= 0.8 else "fail", "page": i + 1,
                               "detail": "only %.1f%% of the words of a plain extraction appear in the parse" % (100 * rec),
                               "missing_sample": [w for w, _ in miss.most_common(10)]})
            if tot < 20 and i < len(doc["pages"]) and doc["pages"][i]["kind"] == "text" and not doc["pages"][i]["ocr"]:
                issues.append({"check": "empty_page", "severity": "warn", "page": i + 1,
                               "detail": "page has almost no text (<20 words): blank, image-only or needs OCR"})

    # ---- 2. duplicated paragraphs
    seen = Counter(re.sub(r"\s+", " ", b["text"]).strip() for b in blocks if b["type"] == "paragraph" and len(b["text"]) >= 80)
    dups = [t[:80] for t, c in seen.items() if c > 1]
    if dups:
        issues.append({"check": "duplicates", "severity": "warn", "detail": "%d paragraph(s) appear more than once" % len(dups), "sample": dups[:3]})

    # ---- 3. furniture leakage
    furn = set()
    for k in ("headers", "footers"):
        for f in doc.get("furniture", {}).get(k, []):
            if len(f["text"]) >= 12:
                furn.add(f["text"].strip())
    leaked = [b["id"] for b in blocks if b["type"] == "paragraph" and any(f in b["text"] for f in furn)]
    if leaked:
        issues.append({"check": "furniture_leak", "severity": "warn", "detail": "running header/footer text found inside body blocks", "blocks": leaked[:5]})

    # ---- 4. order inversions (same column: next block starts above previous)
    inv = []
    body = [b for b in blocks if b["type"] in ("paragraph", "heading", "list_item", "caption", "formula") and not b.get("pages")]
    for p, q in zip(body, body[1:]):
        if p["page"] != q["page"]:
            continue
        ox = min(p["bbox"][2], q["bbox"][2]) - max(p["bbox"][0], q["bbox"][0])
        if ox > 0.5 * min(p["bbox"][2] - p["bbox"][0], q["bbox"][2] - q["bbox"][0]) and q["bbox"][3] < p["bbox"][1] - 2:
            inv.append([p["id"], q["id"]])
    if inv:
        issues.append({"check": "order_inversion", "severity": "warn" if len(inv) < 4 else "fail",
                       "detail": "%d block pair(s) read upwards inside one column" % len(inv), "pairs": inv[:5]})

    # ---- 5. garbled layer
    for i, pg in enumerate(doc["pages"]):
        txt = " ".join(b["text"] for b in blocks if b["page"] == i + 1)
        n = len(txt) or 1
        bad = txt.count("�") + txt.count("(cid:") + len(re.findall(r"[-]", txt))
        if bad / n > 0.02:
            issues.append({"check": "garbled", "severity": "fail", "page": i + 1,
                           "detail": "%.1f%% unmappable glyphs (U+FFFD/cid/private-use): text layer is unreliable" % (100.0 * bad / n)})
    for u in doc.get("unparsed", []):
        issues.append({"check": "unparsed", "severity": "fail", "page": u.get("page"), "detail": "%s %s" % (u["reason"], u.get("detail", ""))})

    # ---- 6. numbered headings gaps
    nums = []
    for b in blocks:
        if b["type"] == "heading" and b.get("number") and re.match(r"^\d+$", b["number"]):
            nums.append(int(b["number"]))
    if nums:
        missing = [n for n in range(min(nums), max(nums) + 1) if n not in nums]
        if missing:
            issues.append({"check": "heading_gap", "severity": "warn", "detail": "top-level section numbers missing from the parse: %s" % missing[:8]})

    # ---- 7. tables
    for t in doc.get("tables", []):
        w = {len(r) for r in t["rows"]}
        cells = [c for r in t["rows"] for c in r]
        empty = sum(1 for c in cells if not c)
        if len(w) > 1:
            issues.append({"check": "table_ragged", "severity": "warn", "table": t["id"], "detail": "rows have different column counts %s" % sorted(w)})
        if cells and empty / len(cells) > 0.6:
            issues.append({"check": "table_sparse", "severity": "warn", "table": t["id"], "detail": "%.0f%% empty cells" % (100.0 * empty / len(cells))})

    # ---- verdict
    sev = [i["severity"] for i in issues]
    verdict = "fail" if "fail" in sev else ("warn" if "warn" in sev else "pass")
    if any(i["check"] in ("garbled", "unparsed") for i in issues):
        actions.append("Text layer missing or garbled on some pages: re-run `parse_pdf.py --ocr force` (needs tesseract) and report pages that stay unreadable as unparsed.")
    if any(i["check"] == "recall" and i["severity"] != "info" for i in issues):
        actions.append("Low recall: re-run with `--backend pdfplumber`, then compare; list the missing words as uncertainty if they cannot be recovered.")
    if any(i["check"] == "order_inversion" for i in issues):
        actions.append("Order inversions: open the listed block pairs in parsed.json (bbox) and correct the order manually; state it in change records.")
    if any(i["check"] == "furniture_leak" for i in issues):
        actions.append("Remove the leaked header/footer strings from the listed blocks and record the edit.")
    rec_vals = [p["recall"] for p in per_page if p["tokens"] >= 30]
    summary = {
        "verdict": verdict, "independent_extractor": backend if pages_plain is not None else None,
        "mean_token_recall": round(sum(rec_vals) / len(rec_vals), 4) if rec_vals else None,
        "min_token_recall": min(rec_vals) if rec_vals else None,
        "pages": len(doc["pages"]), "blocks": len(blocks), "tables": len(doc.get("tables", [])),
        "issue_count": len(issues), "recommended_actions": actions,
    }
    report = {"summary": summary, "issues": issues, "per_page_recall": per_page}
    if a.out:
        json.dump(report, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))
    for i in issues[:15]:
        print("-", i["severity"].upper(), i["check"], ("p%s" % i["page"]) if i.get("page") else "", i["detail"])
    return 0 if verdict != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
