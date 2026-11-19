#!/usr/bin/env python3
"""scholarly-pdf-parser: parse a scientific PDF into structured, evidence-linked output.

Usage:
    python3 parse_pdf.py INPUT.pdf --out OUT_DIR [--ocr auto|off|force] [--backend auto|pymupdf|pdfplumber|pypdf]

Writes OUT_DIR/parsed.json, OUT_DIR/parsed.md, OUT_DIR/quality_report.json and prints a one-line JSON
summary on stdout. Every block carries page + bbox so a claim can be traced back to the source page.
Nothing here guesses content: unreadable regions are reported in `unparsed` / `warnings`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict

VERSION = "1.0.0"
SCHEMA = "scholarly-pdf-parser/1"

# ----------------------------------------------------------------------------------------------
# text normalisation helpers
# ----------------------------------------------------------------------------------------------
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
             "\ufb05": "st", "\ufb06": "st", "\u00ad": ""}

# Adobe Symbol font encoding when exposed as private-use U+F0xx (common in older Word/Acrobat PDFs)
SYMBOL_PUA = {
    "a": "α", "b": "β", "c": "χ", "d": "δ", "e": "ε", "f": "φ", "g": "γ", "h": "η", "i": "ι", "k": "κ",
    "l": "λ", "m": "μ", "n": "ν", "o": "ο", "p": "π", "q": "θ", "r": "ρ", "s": "σ", "t": "τ", "u": "υ",
    "w": "ω", "x": "ξ", "y": "ψ", "z": "ζ", "A": "Α", "B": "Β", "C": "Χ", "D": "Δ", "E": "Ε", "F": "Φ",
    "G": "Γ", "H": "Η", "I": "Ι", "K": "Κ", "L": "Λ", "M": "Μ", "N": "Ν", "P": "Π", "Q": "Θ", "R": "Ρ",
    "S": "Σ", "T": "Τ", "W": "Ω", "X": "Ξ", "Y": "Ψ", "Z": "Ζ",
    "\xb1": "±", "\xb4": "×", "\xb7": "·", "\xa3": "≤", "\xb3": "≥", "\xb9": "≠", "\xb5": "∝", "\xb0": "°",
    "\xa5": "∞", "\xd6": "√", "\xb6": "∂", "\xd1": "∇", "\xe5": "∑", "\xf2": "∫", "\xae": "→", "\xab": "↔",
    "\xac": "←", "\xbb": "≈", "\xba": "≡", "\xce": "∈", "\xc8": "∪", "\xc7": "∩", "\xd7": "⋅", "\xb2": "″",
}
MATH_FONT_RE = re.compile(r"(Math|CMMI|CMSY|CMEX|CMR|MSAM|MSBM|Symbol|Cambria-?Math|STIX|MTExtra|MT-?Extra|Euclid)", re.I)
TERMINAL = tuple(".?!:;。？！：；”\"')]")


def clean_text(s: str) -> str:
    """Expand ligatures, decode Symbol-PUA, fold math-alphanumerics; leave everything else intact."""
    if not s:
        return s
    out = []
    for ch in s:
        o = ord(ch)
        if ch in LIGATURES:
            out.append(LIGATURES[ch])
        elif 0xF020 <= o <= 0xF0FF:
            out.append(SYMBOL_PUA.get(chr(o - 0xF000), ch))
        elif 0x1D400 <= o <= 0x1D7FF:  # mathematical alphanumerics -> plain letters/digits
            out.append(unicodedata.normalize("NFKC", ch))
        elif ch in "\u200b\ufeff\u2060":
            continue
        elif ch == "\u00a0" or ch == "\u2009" or ch == "\u202f":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", s.lower())).strip()


def rect_w(b):
    return b[2] - b[0]


def rect_h(b):
    return b[3] - b[1]


def union(bs):
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


def inter_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def area(b):
    return max(0.0, rect_w(b)) * max(0.0, rect_h(b))


def r2(b):
    return [round(float(x), 1) for x in b]


# ----------------------------------------------------------------------------------------------
# backends: every backend yields per page: dict(width,height,lines=[...],images=[bbox],drawings=[bbox],
# chars=int, tables=[...]). A "line" is a dict: bbox,text,marked,size,bold,math,block,dir.
# ----------------------------------------------------------------------------------------------
class Backend:
    name = "none"

    def __init__(self, path):
        self.path = path

    def page_count(self):
        raise NotImplementedError

    def metadata(self):
        return {}

    def page(self, i):  # -> dict
        raise NotImplementedError

    def render_png(self, i, dpi, out_path):
        return False

    def tables(self, i, page_info):
        return []


class PyMuPDFBackend(Backend):
    name = "pymupdf"

    def __init__(self, path):
        super().__init__(path)
        try:
            import pymupdf as fitz  # type: ignore
        except Exception:  # pragma: no cover
            import fitz  # type: ignore
        self.fitz = fitz
        self.doc = fitz.open(path)
        self.encrypted = bool(self.doc.needs_pass)
        if self.encrypted:
            if not self.doc.authenticate(""):
                raise PermissionError("encrypted PDF (password required)")

    def page_count(self):
        return len(self.doc)

    def metadata(self):
        try:
            m = self.doc.metadata or {}
            return {k: v for k, v in m.items() if v}
        except Exception:
            return {}

    def page(self, i):
        fitz = self.fitz
        p = self.doc[i]
        rect = p.rect
        flags = fitz.TEXTFLAGS_DICT & ~getattr(fitz, "TEXT_PRESERVE_IMAGES", 0)
        d = p.get_text("dict", flags=flags)
        lines = []
        chars = 0
        for bi, blk in enumerate(d.get("blocks", [])):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                spans = [s for s in ln.get("spans", []) if s.get("text") is not None]
                raw = "".join(s["text"] for s in spans)
                if not raw.strip():
                    continue
                sizes = [s["size"] for s in spans if s["text"].strip()]
                size = max(sizes) if sizes else 0
                big = [s for s in spans if s["text"].strip() and s["size"] >= 0.9 * size]
                base_y = statistics.median([s["origin"][1] for s in big]) if big else ln["bbox"][3]
                plain, marked = [], []
                bold_chars = mchars = tot = 0
                for s in spans:
                    t = clean_text(s["text"])
                    plain.append(t)
                    n = len(s["text"].strip())
                    tot += n
                    fn = s.get("font", "")
                    if (s.get("flags", 0) & 16) or re.search(r"bold|black|heavy|semibold|-bd|\.b$", fn, re.I):
                        bold_chars += n
                    if MATH_FONT_RE.search(fn):
                        mchars += n
                    small = s["size"] < 0.8 * size
                    oy = s["origin"][1]
                    if t.strip() and (s.get("flags", 0) & 1 or (small and oy < base_y - 0.18 * size)):
                        marked.append("^{" + t + "}")
                    elif t.strip() and small and oy > base_y + 0.12 * size:
                        marked.append("_{" + t + "}")
                    else:
                        marked.append(t)
                text = "".join(plain)
                mk = "".join(marked)
                chars += len(text.strip())
                lines.append({
                    "bbox": list(ln["bbox"]), "text": text.rstrip(), "marked": mk.rstrip() if mk != text else None,
                    "size": round(size, 2), "bold": (bold_chars / tot) if tot else 0.0,
                    "math": (mchars / tot) if tot else 0.0, "block": bi, "dir": tuple(ln.get("dir", (1, 0))),
                })
        images = []
        try:
            for info in p.get_image_info():
                images.append(list(info["bbox"]))
        except Exception:
            pass
        drawings = []
        try:
            drawings = [list(r) for r in p.cluster_drawings()]
        except Exception:
            pass
        return {"width": rect.width, "height": rect.height, "lines": lines, "images": images,
                "drawings": drawings, "chars": chars}

    def render_png(self, i, dpi, out_path):
        pix = self.doc[i].get_pixmap(dpi=dpi)
        pix.save(out_path)
        return True

    # --- tables ---------------------------------------------------------------------------------
    def _chars(self, p):
        out = []
        try:
            raw = p.get_text("rawdict")
        except Exception:
            return out
        for b in raw.get("blocks", []):
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                for sp in l.get("spans", []):
                    for c in sp.get("chars", []):
                        out.append((c["bbox"], c["c"]))
        return out

    @staticmethod
    def _text_in(chars, rect, want_wide=False):
        """Text of the characters whose centre lies in rect, ordered by GEOMETRY (not by content-stream order, which
        scrambles decimals such as 80.6 in LaTeX tables). Also reports whether a line has a column-sized internal gap."""
        ch = [c for c in chars if rect[0] - 0.5 <= (c[0][0] + c[0][2]) / 2 <= rect[2] + 0.5
              and rect[1] - 0.5 <= (c[0][1] + c[0][3]) / 2 <= rect[3] + 0.5]
        if not ch:
            return ("", 0, 0) if want_wide else ""
        ch.sort(key=lambda c: ((c[0][1] + c[0][3]) / 2, c[0][0]))
        lines, cur = [], [ch[0]]
        for c in ch[1:]:
            if abs((c[0][1] + c[0][3]) / 2 - (cur[-1][0][1] + cur[-1][0][3]) / 2) < 3:
                cur.append(c)
            else:
                lines.append(cur)
                cur = [c]
        lines.append(cur)
        out, wide = [], 0
        for ln in lines:
            ln.sort(key=lambda c: c[0][0])
            txt, prev = "", None
            for c in ln:
                h = max(c[0][3] - c[0][1], 1)
                if prev is not None:
                    gap = c[0][0] - prev
                    if gap > 0.25 * h and not txt.endswith(" "):
                        txt += " "
                    if gap > 0.85 * h and gap > 8:
                        wide += 1
                txt += c[1]
                prev = c[0][2]
            out.append(txt.strip())
        text = clean_text(" ".join(x for x in out if x))
        return (text, wide, len(out)) if want_wide else text

    def _word_grid(self, chars, bbox):
        """Rebuild a borderless / booktabs table from word positions: rows = baselines, columns = vertical white gaps."""
        inside = [c for c in chars if bbox[0] - 1 <= (c[0][0] + c[0][2]) / 2 <= bbox[2] + 1 and bbox[1] - 1 <= (c[0][1] + c[0][3]) / 2 <= bbox[3] + 1]
        if len(inside) < 12:
            return []
        inside.sort(key=lambda c: ((c[0][1] + c[0][3]) / 2, c[0][0]))
        med_h = statistics.median([c[0][3] - c[0][1] for c in inside if c[1].strip()] or [9])
        rows, cur = [], [inside[0]]
        for c in inside[1:]:
            if abs((c[0][1] + c[0][3]) / 2 - statistics.mean([(x[0][1] + x[0][3]) / 2 for x in cur])) < 0.55 * med_h:
                cur.append(c)
            else:
                rows.append(cur)
                cur = [c]
        rows.append(cur)
        words_by_row = []
        for r in rows:
            r.sort(key=lambda c: c[0][0])
            words, w, prev = [], [], None
            for c in r:
                if c[1].isspace():
                    prev = c[0][2]
                    if w:
                        words.append(w)
                        w = []
                    continue
                if prev is not None and w and c[0][0] - prev > 0.28 * med_h:
                    words.append(w)
                    w = []
                w.append(c)
                prev = c[0][2]
            if w:
                words.append(w)
            words_by_row.append([(min(x[0][0] for x in w), max(x[0][2] for x in w), "".join(x[1] for x in w)) for w in words])
        # drop caption rows (they are not table content)
        keep = []
        for r in words_by_row:
            line = " ".join(w[2] for w in r)
            if CAPTION_RE.match(line) or CAPTION_LOOSE_RE.match(line):
                continue
            keep.append(r)
        words_by_row = [r for r in keep if r]
        if len(words_by_row) < 2:
            return []
        x0 = min(w[0] for r in words_by_row for w in r)
        x1 = max(w[1] for r in words_by_row for w in r)
        W = int(x1 - x0) + 2
        cov = [0] * W
        for r in words_by_row:
            seen = set()
            for w in r:
                for x in range(int(w[0] - x0), int(w[1] - x0) + 1):
                    if 0 <= x < W and x not in seen:
                        cov[x] += 1
                        seen.add(x)
        n = len(words_by_row)
        thr = max(1 if n >= 4 else 0, int(0.15 * n))
        gaps, start = [], None
        for x in range(W + 1):
            low = x < W and cov[x] <= thr
            if low and start is None:
                start = x
            elif not low and start is not None:
                if x - start >= 5:
                    gaps.append((x0 + start, x0 + x))
                start = None
        edges = [x0 - 1] + [(g[0] + g[1]) / 2 for g in gaps if g[0] > x0 + 1 and g[1] < x1 - 1] + [x1 + 1]
        ncols = len(edges) - 1
        if ncols < 2:
            return []
        grid = []
        for r in words_by_row:
            cells = [[] for _ in range(ncols)]
            for w in r:
                xc = (w[0] + w[1]) / 2
                k = max(0, min(ncols - 1, next((i for i in range(ncols) if edges[i] <= xc < edges[i + 1]), ncols - 1)))
                cells[k].append(w[2])
            grid.append([clean_text(" ".join(c)) for c in cells])
        grid = [g for g in grid if any(g)]
        # drop all-empty columns
        keepc = [j for j in range(ncols) if any(g[j] for g in grid)]
        grid = [[g[j] for j in keepc] for g in grid]
        wide_gaps = [g for g in gaps if g[1] - g[0] >= 22 and g[0] > x0 + 20 and g[1] < x1 - 20]
        tables = [grid]
        if wide_gaps and len(keepc) >= 4:
            # side-by-side tables: split at the widest white gap that separates >=2 columns on each side
            wg = max(wide_gaps, key=lambda g: g[1] - g[0])
            mid = (wg[0] + wg[1]) / 2
            left_cols = [j for j in keepc if edges[j + 1] <= mid]
            right_cols = [j for j in keepc if edges[j] >= mid]
            if len(left_cols) >= 2 and len(right_cols) >= 2:
                li = [keepc.index(j) for j in left_cols]
                ri = [keepc.index(j) for j in right_cols]
                first = lambda idx: next((g[idx[0]] for g in grid if g[idx[0]]), "")
                # two tables only when the halves repeat the same first-column header, or the gap is very wide
                if not (first(li).strip().lower() == first(ri).strip().lower() or (wg[1] - wg[0]) >= 40):
                    return tables
                tables = [[[g[j] for j in li] for g in grid if any(g[j] for j in li)],
                          [[g[j] for j in ri] for g in grid if any(g[j] for j in ri)]]
                tables = [t for t in tables if len(t) >= 2]
        return tables

    def tables(self, i, page_info):
        out = []
        p = self.doc[i]
        try:
            finder = p.find_tables()
        except Exception:
            return out
        chars = None
        for t in finder.tables:
            if chars is None:
                chars = self._chars(p)
            grid, wide_cells, long_cells, caption_in, stacked = [], 0, 0, False, 0
            try:
                for row in t.rows:
                    r = []
                    for c in row.cells:
                        if c is None:
                            r.append(None)
                            continue
                        txt, w, nl = self._text_in(chars, c, True)
                        wide_cells += 1 if w else 0
                        stacked += 1 if nl >= 3 else 0
                        long_cells += 1 if len(txt) > 110 else 0
                        caption_in = caption_in or bool(CAPTION_RE.match(txt))
                        r.append(txt)
                    grid.append(r)
            except Exception:
                grid = []
            if not grid:
                try:
                    grid = [[("" if c is None else clean_text(str(c))) for c in r] for r in t.extract()]
                except Exception:
                    continue
            ncols = max((len(r) for r in grid), default=0)
            degenerate = wide_cells >= 2 or long_cells >= 1 or stacked >= 1 or caption_in or (ncols <= 2 and len(grid) <= 6 and sum(len(c or "") for r in grid for c in r) > 150)
            if degenerate:
                rebuilt = self._word_grid(chars, list(t.bbox))
                if rebuilt:
                    for k, g in enumerate(rebuilt):
                        out.append({"bbox": list(t.bbox), "rows": g, "cells": [], "strategy": "words",
                                    "note": "rebuilt from word positions (ruled grid was unreliable)" + ("; split from side-by-side tables" if len(rebuilt) > 1 else "")})
                    continue
            if len(grid) < 2 or ncols < 2:
                continue
            cells = [list(c) for c in getattr(t, "cells", []) if c]
            out.append({"bbox": list(t.bbox), "rows": grid, "cells": cells, "strategy": "lines"})
        return out


class PdfplumberBackend(Backend):
    name = "pdfplumber"

    def __init__(self, path):
        super().__init__(path)
        import pdfplumber  # type: ignore
        self.pdf = pdfplumber.open(path)
        self.encrypted = False

    def page_count(self):
        return len(self.pdf.pages)

    def metadata(self):
        try:
            return {k: str(v) for k, v in (self.pdf.metadata or {}).items() if v}
        except Exception:
            return {}

    def page(self, i):
        p = self.pdf.pages[i]
        words = p.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False, use_text_flow=False, x_tolerance=1.2)
        words.sort(key=lambda w: (round(w["top"] / 2), w["x0"]))
        rows = []
        for w in words:
            placed = False
            for r in rows:
                if abs(r["top"] - w["top"]) <= 0.4 * max(w["size"], 1) and 0 <= w["x0"] - r["x1"] <= 0.85 * w["size"]:
                    r["words"].append(w)
                    r["x1"] = w["x1"]
                    r["bottom"] = max(r["bottom"], w["bottom"])
                    placed = True
                    break
            if not placed:
                rows.append({"top": w["top"], "bottom": w["bottom"], "x0": w["x0"], "x1": w["x1"], "words": [w]})
        rows.sort(key=lambda r: (r["top"], r["x0"]))
        lines, chars, open_blocks, next_blk = [], 0, [], 0
        for r in rows:
            ws = r["words"]
            text = clean_text(" ".join(w["text"] for w in ws))
            size = max(w["size"] for w in ws)
            tot = max(1, sum(len(w["text"]) for w in ws))
            bold = sum(len(w["text"]) for w in ws if re.search(r"bold|black", w["fontname"], re.I)) / tot
            math = sum(len(w["text"]) for w in ws if MATH_FONT_RE.search(w["fontname"])) / tot
            bbox = [r["x0"], r["top"], r["x1"], r["bottom"]]
            # attach the row to the block whose last row sits directly above it in the same column
            best, bgap = None, 1e9
            for ob in open_blocks:
                lr = ob["last"]
                gap = r["top"] - lr["bottom"]
                ov = min(r["x1"], lr["x1"]) - max(r["x0"], lr["x0"])
                if -2 * size <= gap <= 0.7 * size and ov >= 0.3 * min(r["x1"] - r["x0"], lr["x1"] - lr["x0"]) and gap < bgap:
                    best, bgap = ob, gap
            if best is None:
                best = {"id": next_blk, "last": r}
                next_blk += 1
                open_blocks.append(best)
            best["last"] = r
            chars += len(text.strip())
            lines.append({"bbox": bbox, "text": text, "marked": None, "size": round(size, 2), "bold": bold,
                          "math": math, "block": best["id"], "dir": (1, 0)})
        images = [[im["x0"], im["top"], im["x1"], im["bottom"]] for im in (p.images or [])]
        return {"width": float(p.width), "height": float(p.height), "lines": lines, "images": images,
                "drawings": [], "chars": chars}

    def tables(self, i, page_info):
        out = []
        try:
            for t in self.pdf.pages[i].find_tables():
                rows = t.extract()
                if rows and len(rows) >= 2 and len(rows[0]) >= 2:
                    out.append({"bbox": list(t.bbox), "rows": rows, "cells": [list(c) for c in t.cells if c],
                                "strategy": "lines"})
        except Exception:
            pass
        return out


class PypdfBackend(Backend):
    """Degraded backend: text only, no geometry. Reading order and structure are NOT verified."""
    name = "pypdf"

    def __init__(self, path):
        super().__init__(path)
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            from PyPDF2 import PdfReader  # type: ignore
        self.r = PdfReader(path)
        self.encrypted = bool(self.r.is_encrypted)
        if self.encrypted:
            try:
                self.r.decrypt("")
            except Exception:
                raise PermissionError("encrypted PDF (password required)")

    def page_count(self):
        return len(self.r.pages)

    def page(self, i):
        p = self.r.pages[i]
        try:
            w, h = float(p.mediabox.width), float(p.mediabox.height)
        except Exception:
            w, h = 612.0, 792.0
        txt = clean_text(p.extract_text() or "")
        lines, y = [], 0.0
        for k, t in enumerate(txt.split("\n")):
            if t.strip():
                lines.append({"bbox": [0, y, w, y + 10], "text": t.strip(), "marked": None, "size": 10.0, "bold": 0.0,
                              "math": 0.0, "block": k, "dir": (1, 0)})
                y += 10
        return {"width": w, "height": h, "lines": lines, "images": [], "drawings": [], "chars": len(txt.strip()),
                "degraded": True}


def open_backend(path, want):
    errors = []
    order = {"auto": ["pymupdf", "pdfplumber", "pypdf"]}.get(want, [want])
    for name in order:
        try:
            if name == "pymupdf":
                return PyMuPDFBackend(path), errors
            if name == "pdfplumber":
                return PdfplumberBackend(path), errors
            if name == "pypdf":
                return PypdfBackend(path), errors
        except PermissionError:
            raise
        except Exception as e:  # missing library or unreadable file
            errors.append("%s: %s" % (name, str(e)[:160]))
    return None, errors


# ----------------------------------------------------------------------------------------------
# OCR (optional, only for pages without a usable text layer)
# ----------------------------------------------------------------------------------------------
_RAPID = None
OCR_ENGINE_PREF = "auto"


def _rapid_module():
    """'v3' (pip rapidocr, word boxes) or 'v1' (rapidocr-onnxruntime) or None."""
    try:
        import rapidocr  # type: ignore  # noqa: F401
        return "v3"
    except Exception:
        pass
    try:
        import rapidocr_onnxruntime  # type: ignore  # noqa: F401
        return "v1"
    except Exception:
        return None


def _is_cjk(ch):
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F


def _join_words(items, line_h):
    """Join word-level OCR tokens [(text, x0, x1)]. Real words (>= 2 chars on either side) are always space-separated;
    single-character tokens (the engine splits CJK lines and digits per character) are glued unless the boxes are a
    word-space apart; nothing is spaced next to CJK characters (Chinese has no spaces)."""
    out, prev_x1, prev_tok = "", None, ""
    for text, x0, x1 in items:
        if out and not (_is_cjk(out[-1]) or _is_cjk(text[0])):
            gap = x0 - prev_x1
            if len(prev_tok) >= 2 or len(text) >= 2 or gap >= 0.15 * line_h:
                out += " "
        out += text
        prev_x1, prev_tok = x1, text
    return out


def find_ocr():
    """OCR engine to use: 'rapidocr' (pip, bundled CJK+Latin models), 'tesseract' (CLI) or 'pytesseract'."""
    pref = OCR_ENGINE_PREF
    if pref in ("auto", "rapidocr"):
        if _rapid_module() is not None:
            return "rapidocr"
        if pref == "rapidocr":
            return None
    if pref in ("auto", "tesseract") and shutil.which("tesseract"):
        return "tesseract"
    if pref in ("auto", "pytesseract"):
        try:
            import pytesseract  # type: ignore  # noqa: F401,F811
            return "pytesseract"
        except Exception:
            return None
    return None


def normalise_ocr_sizes(lines):
    """OCR boxes include descenders/ascenders, so raw heights jitter +-20 %. Snap near-median lines to the median so only
    clearly larger lines (titles, headings) differ from body text."""
    hs = sorted(l["size"] for l in lines if len(l["text"]) >= 12)
    if len(hs) < 5:
        return
    med = hs[len(hs) // 2]
    for l in lines:
        if 0.7 * med <= l["size"] <= 1.3 * med:
            l["size"] = med


def assign_blocks(lines):
    """Give OCR lines (which carry no paragraph info) block ids: a line joins the block whose last line sits right above it."""
    open_blocks, nxt = [], 0
    for ln in sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0])):
        size = max(ln["size"], 1)
        best, bgap = None, 1e9
        for ob in open_blocks:
            lr = ob["last"]
            gap = ln["bbox"][1] - lr["bbox"][3]
            ov = min(ln["bbox"][2], lr["bbox"][2]) - max(ln["bbox"][0], lr["bbox"][0])
            if -0.5 * size <= gap <= 0.9 * size and ov >= 0.3 * min(ln["bbox"][2] - ln["bbox"][0], lr["bbox"][2] - lr["bbox"][0]) and gap < bgap:
                best, bgap = ob, gap
        if best is None:
            best = {"id": nxt, "last": ln}
            nxt += 1
            open_blocks.append(best)
        best["last"] = ln
        ln["block"] = best["id"]
    return lines


def ocr_page(backend, i, lang, dpi=None):
    """Return (lines, info). Renders the page and runs the selected engine. Never invents text."""
    tool = find_ocr()
    if tool is None:
        return None, "no OCR engine found (rapidocr-onnxruntime / tesseract / pytesseract)"
    dpi = dpi or (200 if tool == "rapidocr" else 300)
    tmp = tempfile.mkdtemp(prefix="ocr_")
    try:
        png = os.path.join(tmp, "p.png")
        if not backend.render_png(i, dpi, png):
            return None, "backend cannot render pages"
        scale = 72.0 / dpi
        lines = []
        if tool == "rapidocr":
            global _RAPID
            ver = _rapid_module()
            if _RAPID is None:
                import logging
                logging.getLogger("RapidOCR").setLevel(logging.WARNING)
                if ver == "v3":
                    from rapidocr import RapidOCR  # type: ignore
                else:
                    from rapidocr_onnxruntime import RapidOCR  # type: ignore
                try:
                    _RAPID = RapidOCR(params={"Global.log_level": "warning"}) if ver == "v3" else RapidOCR()
                except Exception:
                    _RAPID = RapidOCR()
            if ver == "v3":
                out = _RAPID(png, return_word_box=True)
                wr = getattr(out, "word_results", None) or []
                scores = list(getattr(out, "scores", None) or [])
                for k, wline in enumerate(wr):
                    words = [w for w in wline if str(w[0]).strip()]
                    if not words:
                        continue
                    xs = [pt[0] for w in words for pt in w[2]]
                    ys = [pt[1] for w in words for pt in w[2]]
                    x0, y0, x1, y1 = min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale
                    conf = scores[k] if k < len(scores) else (sum(float(w[1]) for w in words) / len(words))
                    items = sorted(((str(w[0]).strip(), min(pt[0] for pt in w[2]), max(pt[0] for pt in w[2])) for w in words), key=lambda t: t[1])
                    lines.append({"bbox": [x0, y0, x1, y1], "text": clean_text(_join_words(items, max(ys) - min(ys))),
                                  "marked": None, "size": round((y1 - y0) * 0.9, 2), "bold": 0.0, "math": 0.0, "block": 0,
                                  "dir": (1, 0), "ocr_conf": round(float(conf) * 100, 1)})
            else:
                res, _ = _RAPID(png)
                for box, text, score in (res or []):
                    if not str(text).strip():
                        continue
                    xs = [pt[0] for pt in box]
                    ys = [pt[1] for pt in box]
                    x0, y0, x1, y1 = min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale
                    lines.append({"bbox": [x0, y0, x1, y1], "text": clean_text(str(text).strip()), "marked": None,
                                  "size": round((y1 - y0) * 0.9, 2), "bold": 0.0, "math": 0.0, "block": 0, "dir": (1, 0),
                                  "ocr_conf": round(float(score) * 100, 1)})
            for l in lines:  # tall, narrow boxes are rotated text (arXiv stamp, axis labels): treat as margin text
                if (l["bbox"][3] - l["bbox"][1]) > 2.5 * (l["bbox"][2] - l["bbox"][0]) and len(l["text"]) >= 3:
                    l["dir"] = (0, -1)
            normalise_ocr_sizes([l for l in lines if l["dir"] == (1, 0)])
            assign_blocks(lines)
            return lines, "ocr via rapidocr %s (bundled Chinese+English models; --lang is ignored)" % ver
        if tool == "tesseract":
            cmd = ["tesseract", png, "stdout", "-l", lang, "--psm", "1", "tsv"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if res.returncode != 0 and lang != "eng":
                cmd[4] = "eng"
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if res.returncode != 0:
                return None, "tesseract failed: " + res.stderr[:160]
            tsv = res.stdout
        else:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
            tsv = pytesseract.image_to_data(Image.open(png), lang=lang, output_type=pytesseract.Output.STRING)
        groups = defaultdict(list)
        for row in tsv.split("\n")[1:]:
            c = row.split("\t")
            if len(c) < 12 or not c[11].strip():
                continue
            try:
                conf = float(c[10])
            except ValueError:
                continue
            groups[(c[2], c[3], c[4])].append((int(c[6]), int(c[7]), int(c[8]), int(c[9]), conf, c[11]))
        for key, ws in sorted(groups.items(), key=lambda kv: (int(kv[0][0]), int(kv[0][1]), int(kv[0][2]))):
            x0 = min(w[0] for w in ws) * scale
            y0 = min(w[1] for w in ws) * scale
            x1 = max(w[0] + w[2] for w in ws) * scale
            y1 = max(w[1] + w[3] for w in ws) * scale
            conf = [w[4] for w in ws if w[4] >= 0]
            lines.append({"bbox": [x0, y0, x1, y1], "text": clean_text(" ".join(w[5] for w in ws)), "marked": None,
                          "size": round((y1 - y0) * 0.85, 2), "bold": 0.0, "math": 0.0, "block": int(key[0]) * 1000 + int(key[1]),
                          "dir": (1, 0), "ocr_conf": round(sum(conf) / len(conf), 1) if conf else None})
        return lines, "ocr via " + tool
    except subprocess.TimeoutExpired:
        return None, "OCR timed out"
    except Exception as e:
        return None, "OCR error: " + str(e)[:160]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------------------------
# layout: furniture, figures, columns (recursive XY-cut), paragraphs
# ----------------------------------------------------------------------------------------------
PAGE_NO_RE = re.compile(r"^(page\s*)?#{1,4}(\s*(of|/)\s*#{1,4})?$|^[-–—]\s*#{1,4}\s*[-–—]$|^#{1,4}\s*$")


def detect_furniture(pages):
    """Mark lines in the top/bottom margins that repeat across pages (running heads, page numbers)."""
    n = len(pages)
    keys = defaultdict(set)
    for pi, pg in enumerate(pages):
        H = pg["height"]
        for ln in pg["lines"]:
            ln["zone"] = None
            y0, y1 = ln["bbox"][1], ln["bbox"][3]
            if y1 <= 0.095 * H:
                ln["zone"] = "header"
            elif y0 >= 0.905 * H:
                ln["zone"] = "footer"
            if ln["zone"]:
                keys[norm_key(ln["text"])].add(pi)
    furn_keys = set()
    for k, pgs in keys.items():
        if PAGE_NO_RE.match(k) or (n >= 3 and len(pgs) >= max(2, int(0.3 * n + 0.5))) or (n == 2 and len(pgs) == 2 and len(k) > 3):
            furn_keys.add(k)
    for pg in pages:
        for ln in pg["lines"]:
            ln["furniture"] = bool(ln.get("zone") and norm_key(ln["text"]) in furn_keys)
            # rotated text (arXiv stamp, margin notes)
            dx, dy = ln["dir"]
            ln["rotated"] = abs(dy) > 0.5 or dx < -0.5
    return furn_keys


def vertical_groups(items, min_gap):
    """Split items into left-to-right groups separated by an x-gap free of any item."""
    if len(items) < 2:
        return None
    iv = sorted(items, key=lambda it: it["bbox"][0])
    groups, cur, reach = [], [iv[0]], iv[0]["bbox"][2]
    for it in iv[1:]:
        if it["bbox"][0] - reach >= min_gap:
            groups.append(cur)
            cur = [it]
        else:
            cur.append(it)
        reach = max(reach, it["bbox"][2])
    groups.append(cur)
    if len(groups) < 2:
        return None
    # ignore slivers: every group needs real text mass (>=2 lines), otherwise it is margin noise
    heavy = [g for g in groups if sum(len(i["lines"]) for i in g) >= 2]
    if len(heavy) < 2:
        return None
    return groups


def find_gutters(items, gap):
    """x-intervals (inside the region) that almost no text line covers: column gutters."""
    x0 = min(i["bbox"][0] for i in items)
    x1 = max(i["bbox"][2] for i in items)
    W = int(x1 - x0) + 2
    cov = [0.0] * W
    for it in items:
        for l in it["lines"]:
            a, b = int(l["bbox"][0] - x0), int(l["bbox"][2] - x0) + 1
            h = l["bbox"][3] - l["bbox"][1]
            for x in range(max(0, a), min(W, b)):
                cov[x] += h
    mx = max(cov) if cov else 0
    if mx <= 0:
        return []
    thr = 0.22 * mx
    out, start = [], None
    for x in range(W + 1):
        low = x < W and cov[x] <= thr
        if low and start is None:
            start = x
        elif not low and start is not None:
            if x - start >= gap and start >= 0.12 * W and x <= 0.88 * W:
                out.append((x0 + start, x0 + x))
            start = None
    return out


def xy_order(items, page_w, depth=0):
    """Recursive XY-cut. (1) full-height gutter -> columns; (2) otherwise blocks that cross the dominant
    gutter (title, wide figures, spanning captions, author rows) act as separators between bands."""
    if len(items) <= 1 or depth > 12:
        return list(items)
    x0 = min(i["bbox"][0] for i in items)
    x1 = max(i["bbox"][2] for i in items)
    region_w = max(x1 - x0, 1.0)
    gap = max(6.0, 0.012 * page_w)
    groups = vertical_groups(items, gap)
    if groups:
        out = []
        for g in groups:
            out.extend(xy_order(g, page_w, depth + 1))
        return out
    gutters = find_gutters(items, gap)
    if gutters:
        crossers = [i for i in items if any(min(i["bbox"][2], g1) - max(i["bbox"][0], g0) > 0.5 * (g1 - g0) for g0, g1 in gutters)]
    else:
        crossers = [i for i in items if rect_w(i["bbox"]) >= 0.62 * region_w]
    if crossers and len(crossers) < len(items):
        crossers.sort(key=lambda i: i["bbox"][1])
        bands = [[] for _ in range(len(crossers) + 1)]
        ids = {id(c) for c in crossers}
        for it in items:
            if id(it) in ids:
                continue
            yc = (it["bbox"][1] + it["bbox"][3]) / 2
            k = sum(1 for c in crossers if (c["bbox"][1] + c["bbox"][3]) / 2 < yc)
            bands[k].append(it)
        out = []
        for k, band in enumerate(bands):
            if band:
                out.extend(xy_order(band, page_w, depth + 1))
            if k < len(crossers):
                out.append(crossers[k])
        return out
    return sorted(items, key=lambda i: (round(i["bbox"][1], 0), i["bbox"][0]))


MARKER_RE = re.compile(r"^(\[\d{1,3}\]|\d{1,3}\.|\(\d{1,3}\)|\d{1,2}(\.\d{1,2}){0,3}\.?|[A-H](\.\d{1,2}){0,2}\.?)$")


def merge_rows(lines):
    """Re-join fragments of one printed line. PyMuPDF often emits several 'lines' for one baseline when fonts change
    (italic phrase, small caps, a bare section number). Fragments are merged when they sit on the same baseline and
    the horizontal gap is word-sized -- far smaller than a column gutter -- or when the left one is a bare marker."""
    todo = sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0]))
    changed = True
    guard = 0
    while changed and guard < 6:
        changed, guard = False, guard + 1
        todo.sort(key=lambda l: ((l["bbox"][1] + l["bbox"][3]) / 2, l["bbox"][0]))
        used, out = set(), []
        for i, a in enumerate(todo):
            if i in used:
                continue
            best, bgap = None, 1e9
            for j in range(len(todo)):
                if j == i or j in used:
                    continue
                r = todo[j]
                if r["bbox"][0] < a["bbox"][2] - 0.5:
                    continue
                ov = min(a["bbox"][3], r["bbox"][3]) - max(a["bbox"][1], r["bbox"][1])
                if ov < 0.5 * min(a["bbox"][3] - a["bbox"][1], r["bbox"][3] - r["bbox"][1]):
                    continue
                gap = r["bbox"][0] - a["bbox"][2]
                size = max(a["size"], r["size"], 1)
                marker = bool(MARKER_RE.match(a["text"].strip())) and not MARKER_RE.match(r["text"].strip())
                lim = 3.2 * size if marker else (1.1 * size if a["block"] == r["block"] else 0.45 * size)
                if gap <= lim and gap < bgap:
                    best, bgap = j, gap
            if best is not None:
                r = todo[best]
                used.add(best)
                sep = " " if (bgap > 0.12 * max(a["size"], r["size"], 1) or MARKER_RE.match(a["text"].strip())) else ""
                na, nr = len(a["text"]), len(r["text"])
                merged = dict(a)
                merged.update({
                    "text": a["text"].rstrip() + sep + r["text"].lstrip(),
                    "marked": (((a.get("marked") or a["text"]).rstrip() + sep + (r.get("marked") or r["text"]).lstrip())
                               if (a.get("marked") or r.get("marked")) else None),
                    "bbox": union([a["bbox"], r["bbox"]]), "size": max(a["size"], r["size"]),
                    "bold": (a["bold"] * na + r["bold"] * nr) / max(1, na + nr),
                    "math": (a["math"] * na + r["math"] * nr) / max(1, na + nr)})
                out.append(merged)
                changed = True
            else:
                out.append(a)
        todo = out
    return todo


def make_blocks(lines):
    """Group lines into layout blocks using the backend's block ids, splitting at large vertical gaps."""
    by = defaultdict(list)
    for ln in lines:
        by[ln["block"]].append(ln)
    blocks = []
    for bid, ls in by.items():
        ls.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
        cur = [ls[0]]
        for l in ls[1:]:
            prev = cur[-1]
            gap = l["bbox"][1] - prev["bbox"][3]
            if gap > 0.9 * max(prev["size"], 1) or l["bbox"][0] > prev["bbox"][2] - 1:  # next line right of the previous one => other column
                blocks.append({"lines": cur})
                cur = [l]
            else:
                cur.append(l)
        blocks.append({"lines": cur})
    for b in blocks:
        b["bbox"] = union([l["bbox"] for l in b["lines"]])
    return blocks


def split_paragraphs(block, body_size):
    """Split one layout block into paragraphs at first-line indents / style changes."""
    ls = block["lines"]
    if len(ls) <= 1:
        return [ls]
    x_left = statistics.median([l["bbox"][0] for l in ls])
    x_right = max(l["bbox"][2] for l in ls)
    paras, cur = [], [ls[0]]
    for prev, l in zip(ls, ls[1:]):
        size = max(prev["size"], 1)
        indent = l["bbox"][0] - min(prev["bbox"][0], x_left)
        prev_short = prev["bbox"][2] < x_right - 2.2 * size
        style_change = abs(l["size"] - prev["size"]) > 0.9 or (l["bold"] > 0.6) != (prev["bold"] > 0.6)
        gap = l["bbox"][1] - prev["bbox"][3]
        bigger_gap = gap > 0.55 * size
        if (indent > 0.7 * size and (prev_short or prev["text"].endswith(TERMINAL))) or style_change or \
                (prev_short and prev["text"].endswith(TERMINAL) and bigger_gap) or (bigger_gap and prev_short):
            paras.append(cur)
            cur = [l]
        else:
            cur.append(l)
    paras.append(cur)
    return paras


# ----------------------------------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------------------------------
# strict: label + number + delimiter ("Figure 1." / "Table 2:" / "FIG. 3.") -- a sentence like "Table 3 shows ..." must not match
CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.?|FIGURE|FIG\.?|Table|TABLE|Scheme|Chart|图|表)\s*([A-Z]?\d+[A-Za-z]?(?:\.\d+)?|[IVX]+|[一二三四五六七八九十]+)\s*(?:[.:：|\-–—]|\s{2,}|(?=[一-龥]))", re.U)
CAPTION_LOOSE_RE = re.compile(r"^\s*(Figure|Fig\.?|FIGURE|FIG\.?|Table|TABLE|Scheme|Chart|图|表)\s*([A-Z]?\d+[A-Za-z]?(?:\.\d+)?|[IVX]+)\s+\S", re.U)
NUM_HEAD_RE = re.compile(r"^\s*((?:\d{1,2}(?:\.\d{1,2}){0,3})|(?:[A-H](?:\.\d{1,2}){0,2})|(?:[IVX]{1,5}))[.)]?\s+\S")
KEY_HEAD_RE = re.compile(r"^\s*(abstract|summary|keywords?|index terms|introduction|related work|background|methods?|materials and methods|"
                         r"results?|discussion|conclusions?|acknowledge?ments?|references|bibliography|appendix|supplementary|"
                         r"摘要|关键词|引言|前言|结论|致谢|参考文献|附录)\b", re.I)
REF_HEAD_RE = re.compile(r"^\s*(\d+[.\s]+)?(references?|bibliography|literature cited|参考文献|references and notes)\s*$", re.I)
EQ_NO_RE = re.compile(r"\((\d{1,3}[a-z]?|[A-Z]\d{1,2})\)\s*$")
LIST_RE = re.compile(r"^\s*([•·▪‣◦\-–*]|\(?[a-z0-9]{1,2}[.)])\s+\S")


def weighted_body_size(lines):
    c = Counter()
    for l in lines:
        c[round(l["size"] * 2) / 2] += len(l["text"])
    return c.most_common(1)[0][0] if c else 10.0


def repair_glyphs(pages, warnings):
    """Some journal fonts expose '+' as 'þ' and '=' as '¼'. Repair only with document-level evidence, and report it."""
    ev = 0
    for pg in pages:
        for l in pg["lines"]:
            ev += len(re.findall(r"[\s\d]¼[\s\d]", l["text"]))
    if ev < 2:
        return
    n_eq = n_pl = 0
    for pg in pages:
        for l in pg["lines"]:
            for key in ("text", "marked"):
                v = l.get(key)
                if not v:
                    continue
                if key == "text":
                    n_eq += v.count("¼")
                    n_pl += v.count("þ")
                l[key] = v.replace("¼", "=").replace("þ", "+")
    warnings.append("glyph repair: mapped 'þ'->'+' (%d) and '¼'->'=' (%d) because the font exposes operators under Latin-1 codes "
                    "(%d unambiguous cases in this document); check against the page image" % (n_pl, n_eq, ev))


VOCAB = {"plain": set(), "hyph": set()}


def build_vocab(pages):
    """Words seen inside lines (never at a line end) tell us whether 'high-' + 'level' is a real compound."""
    for pg in pages:
        for l in pg["lines"]:
            toks = re.findall(r"[A-Za-z][A-Za-z']+(?:-[A-Za-z]+)*", l["text"])
            if l["text"].rstrip().endswith("-") and toks:
                toks = toks[:-1]
            for t in toks:
                (VOCAB["hyph"] if "-" in t else VOCAB["plain"]).add(t.lower())


def block_text(lines, key="text"):
    """Join lines with careful de-hyphenation; return (text, n_dehyphenated)."""
    out, n = "", 0
    for l in lines:
        t = (l.get(key) or l["text"]).strip()
        if not out:
            out = t
        elif re.search(r"[A-Za-z]{2,}-$", out) and t[:1].islower():
            a = re.search(r"([A-Za-z]+)-$", out).group(1)
            m = re.match(r"([A-Za-z]+)", t)
            b = m.group(1) if m else ""
            if (a + "-" + b).lower() in VOCAB["hyph"] and (a + b).lower() not in VOCAB["plain"]:
                out += t  # genuine hyphenated compound: keep the hyphen
            else:
                out = out[:-1] + t  # soft hyphen at a line break: rejoin the word
                n += 1
        else:
            out += " " + t
    return re.sub(r"[ \t]{2,}", " ", out).strip(), n


def classify_paragraph(lines, ctx):
    """Return dict(type, level, flags) for a paragraph (list of lines)."""
    text, _ = block_text(lines)
    size = statistics.mean([l["size"] for l in lines])
    bold = sum(l["bold"] * len(l["text"]) for l in lines) / max(1, sum(len(l["text"]) for l in lines))
    math = sum(l["math"] * len(l["text"]) for l in lines) / max(1, sum(len(l["text"]) for l in lines))
    body = ctx["body"]
    flags = []
    n_lines = len(lines)
    bbox = union([l["bbox"] for l in lines])
    H = ctx["height"]
    if CAPTION_RE.match(text) and len(text) > 12:
        return {"type": "caption", "flags": flags}
    # "Table 1 Results ..." without a delimiter: accept only when the label is bold or the text is set smaller than the body
    if CAPTION_LOOSE_RE.match(text) and len(text) > 12 and (lines[0]["bold"] > 0.35 or size < body - 0.6) and n_lines >= 1 \
            and not re.match(r"^\s*\w+\s+\S+\s+(shows?|summari[sz]es?|lists?|presents?|reports?|compares?|illustrates?|gives?|demonstrates?)\b", text):
        return {"type": "caption", "flags": ["caption_no_delimiter"]}
    if REF_HEAD_RE.match(text) and n_lines <= 2:
        return {"type": "heading", "level": 1, "flags": ["references_heading"]}
    short = n_lines <= 2 and len(text) <= 160
    numbered = NUM_HEAD_RE.match(text)
    keyword = KEY_HEAD_RE.match(text) and len(text) < 60
    big = size >= (body * 1.35 if ctx.get("ocr") else body + 0.9)
    if short and (big or (bold > 0.75 and size >= body - 0.6) or (keyword and bold > 0.4)) and not text.endswith((",", ";")):
        if not (text.endswith(".") and len(text) > 90):
            return {"type": "heading", "level": None, "flags": flags, "size": size}
    if short and numbered and (bold > 0.5 or big) and len(text) < 120:
        return {"type": "heading", "level": None, "flags": flags, "size": size}
    # formula: math-font heavy, or symbol-dense short line with equation number on the right
    letters = sum(ch.isalpha() for ch in text)
    symbols = sum((not ch.isalnum()) and not ch.isspace() for ch in text)
    if (math > 0.45 and len(text) < 300) or (EQ_NO_RE.search(text) and n_lines <= 3 and symbols > 0.18 * len(text) and letters < 0.7 * len(text)):
        return {"type": "formula", "flags": flags}
    if size < body - 1.2 and bbox[1] > 0.72 * H and n_lines <= 6 and re.match(r"^\s*([*†‡§¶#]|\d{1,2})\s*\S", text):
        return {"type": "footnote", "flags": flags}
    if LIST_RE.match(text) and size <= body + 0.5:
        return {"type": "list_item", "flags": flags}
    # run-in heading: bold lead-in phrase followed by regular text
    first = lines[0]
    m = re.match(r"^([A-Z][^.:]{2,60}[.:])\s+\S", text)
    if m and first["bold"] > 0.35 and bold < 0.7 and n_lines >= 2:
        flags.append("run_in_heading:" + m.group(1))
    return {"type": "paragraph", "flags": flags}


def parse_number(text):
    m = NUM_HEAD_RE.match(text)
    if not m:
        return None, text.strip()
    num = m.group(1)
    title = text.strip()[len(m.group(0)) - 1:].strip() if False else re.sub(r"^\s*" + re.escape(m.group(1)) + r"[.)]?\s+", "", text).strip()
    return num, title


# ----------------------------------------------------------------------------------------------
# tables
# ----------------------------------------------------------------------------------------------
def grid_from_cells(cells, bbox):
    """Derive rowspan/colspan from cell rectangles. Returns (rows_html_ok, spans) or None."""
    if not cells:
        return None
    xs = sorted({round(c[0], 0) for c in cells} | {round(c[2], 0) for c in cells})
    ys = sorted({round(c[1], 0) for c in cells} | {round(c[3], 0) for c in cells})

    def merge(v):
        out = []
        for a in v:
            if not out or a - out[-1] > 2:
                out.append(a)
        return out
    xs, ys = merge(xs), merge(ys)
    grid = []
    for c in cells:
        cx0 = min(range(len(xs)), key=lambda k: abs(xs[k] - c[0]))
        cx1 = min(range(len(xs)), key=lambda k: abs(xs[k] - c[2]))
        cy0 = min(range(len(ys)), key=lambda k: abs(ys[k] - c[1]))
        cy1 = min(range(len(ys)), key=lambda k: abs(ys[k] - c[3]))
        grid.append((cy0, cx0, max(1, cy1 - cy0), max(1, cx1 - cx0), c))
    return len(ys) - 1, len(xs) - 1, grid


def md_escape(s):
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def table_markdown(rows):
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [[md_escape(c) for c in r] + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def table_html(rows, cells_info, page_words):
    """HTML with colspan/rowspan when the cell geometry tells us about merged cells."""
    if not cells_info:
        return None
    nr, nc, grid = cells_info
    if len(grid) == nr * nc:
        return None  # no merged cells => markdown is enough
    # fill text of each cell from words inside its rectangle
    html = ["<table>"]
    by_row = defaultdict(list)
    for (r, c, rs, cs, rect) in grid:
        by_row[r].append((c, rs, cs, rect))
    for r in range(nr):
        html.append("<tr>")
        for (c, rs, cs, rect) in sorted(by_row.get(r, [])):
            txt = " ".join(w[4] for w in page_words if rect[0] - 1 <= (w[0] + w[2]) / 2 <= rect[2] + 1 and rect[1] - 1 <= (w[1] + w[3]) / 2 <= rect[3] + 1)
            tag = "th" if r == 0 else "td"
            attrs = (' rowspan="%d"' % rs if rs > 1 else "") + (' colspan="%d"' % cs if cs > 1 else "")
            html.append("<%s%s>%s</%s>" % (tag, attrs, md_escape(clean_text(txt)), tag))
        html.append("</tr>")
    html.append("</table>")
    return "".join(html)


def fill_merged(rows):
    """Fill None cells produced by merged/spanning cells with the value of the merged origin (and flag it)."""
    out = []
    for r in rows:
        out.append([("" if c is None else clean_text(str(c)).replace("\n", " ").strip()) for c in r])
    return out


# ----------------------------------------------------------------------------------------------
# main pipeline
# ----------------------------------------------------------------------------------------------
def parse_pdf(path, out_dir, backend_name="auto", ocr_mode="auto", lang="eng", max_pages=0, want_tables=True, ocr_budget=420.0):
    warnings = []
    unparsed = []
    be, errs = open_backend(path, backend_name)
    if be is None:
        raise RuntimeError("no PDF backend available: " + "; ".join(errs))
    for e in errs:
        warnings.append("backend fallback: " + e)
    n_pages = be.page_count()
    if n_pages == 0:
        raise RuntimeError("PDF has no readable pages (truncated, corrupt or empty file)")
    if max_pages and n_pages > max_pages:
        warnings.append("only first %d of %d pages parsed (--max-pages)" % (max_pages, n_pages))
        n_pages = max_pages
    pages = []
    for i in range(n_pages):
        try:
            pg = be.page(i)
        except Exception as e:
            pg = {"width": 612.0, "height": 792.0, "lines": [], "images": [], "drawings": [], "chars": 0}
            warnings.append("page %d: extraction failed (%s)" % (i + 1, str(e)[:120]))
            unparsed.append({"page": i + 1, "reason": "extraction_error"})
        pg["index"] = i
        pages.append(pg)

    # --- text layer assessment / OCR
    import time
    ocr_t0 = time.time()
    page_meta = []
    for pg in pages:
        i = pg["index"]
        page_area = pg["width"] * pg["height"]
        img_cover = sum(area(b) for b in pg["images"]) / page_area if page_area else 0
        garbled = sum(l["text"].count("\ufffd") + l["text"].count("(cid:") for l in pg["lines"])
        total = max(1, pg["chars"])
        kind = "text"
        if pg["chars"] < 40 and (img_cover > 0.35 or not pg["lines"]):
            kind = "scanned"
        elif garbled / total > 0.05:
            kind = "garbled_text_layer"
        pg["kind"], pg["ocr"], pg["ocr_note"] = kind, False, None
        if kind in ("scanned", "garbled_text_layer") and ocr_mode != "off" or ocr_mode == "force":
            if time.time() - ocr_t0 > ocr_budget:
                lines, note = None, "OCR time budget (%ds) exhausted before this page" % ocr_budget
            else:
                lines, note = ocr_page(be, i, lang)
            pg["ocr_note"] = note
            if lines:
                pg["lines"], pg["ocr"] = lines, True
                pg["chars"] = sum(len(l["text"]) for l in lines)
            else:
                warnings.append("page %d needs OCR but it did not run: %s" % (i + 1, note))
                unparsed.append({"page": i + 1, "reason": "needs_ocr", "detail": note})
        elif kind != "text":
            unparsed.append({"page": i + 1, "reason": "needs_ocr", "detail": "OCR disabled (--ocr off)"})
            warnings.append("page %d has no usable text layer and OCR is off" % (i + 1))
        page_meta.append(pg)

    detect_furniture(pages)
    repair_glyphs(pages, warnings)
    build_vocab(pages)

    # --- document-wide body font size
    body_lines = [l for pg in pages for l in pg["lines"] if not l["furniture"] and not l["rotated"]]
    body_size = weighted_body_size(body_lines)

    # --- tables, figures
    for pg in pages:
        pg["tables"] = []
        if want_tables and pg["kind"] == "text" and not pg.get("degraded"):
            try:
                tabs = be.tables(pg["index"], pg)
            except Exception:
                tabs = []
            for t in tabs:
                if area(t["bbox"]) > 0.8 * pg["width"] * pg["height"]:
                    continue
                cells = [c for r in t["rows"] for c in r]
                filled = sum(1 for c in cells if c not in (None, ""))
                # a vector diagram or a ruled text box is not a table: mostly-empty grids and very tall grids are rejected
                def is_prose(txt):  # long, mostly alphabetic, few digits: a table row is numeric-heavy
                    n = len(txt)
                    return n >= 70 and sum(c.isdigit() for c in txt) / n < 0.1 and sum(c.isalpha() or c == " " for c in txt) / n > 0.85
                prose = [l for l in pg["lines"] if is_prose(l["text"]) and inter_area(l["bbox"], t["bbox"]) / (area(l["bbox"]) or 1) > 0.8]
                if len(prose) >= 3:  # running prose inside the region: it is a text area / figure, not a table
                    pg.setdefault("rejected_tables", []).append(list(t["bbox"]))
                    continue
                if not cells or filled / len(cells) < 0.3 or len(t["rows"]) > 60 or len(t["rows"][0]) > 14:
                    pg.setdefault("rejected_tables", []).append(list(t["bbox"]))
                    continue
                pg["tables"].append(t)

    figures_out = []
    bid = 0
    page_infos = []
    for pg in pages:
        i = pg["index"]
        W, H = pg["width"], pg["height"]
        lines = pg["lines"]
        # lines inside tables -> removed from flow
        tbl_boxes = [t["bbox"] for t in pg["tables"]]

        def in_box(l, boxes, frac=0.6):
            a = area(l["bbox"]) or 1.0
            return any(inter_area(l["bbox"], b) / a >= frac for b in boxes)
        # figure regions: images + vector-drawing clusters that carry several short text lines
        fig_rects = []
        for r in pg["images"]:
            # a picture that covers most of the page is a scan / page background, not a figure
            if 0.01 * W * H < area(r) <= 0.7 * W * H:
                fig_rects.append(list(r))

        def grow(r, m):
            return [r[0] - m, r[1] - m, r[2] + m, r[3] + m]
        for r in pg["drawings"]:
            if area(r) > 0.0015 * W * H and area(r) < 0.7 * W * H and not any(inter_area(r, tb) > 0.5 * area(r) for tb in tbl_boxes):
                near = [l for l in lines if inter_area(l["bbox"], grow(r, 24)) / (area(l["bbox"]) or 1) > 0.6
                        and not CAPTION_RE.match(l["text"]) and (l["bbox"][3] - l["bbox"][1]) < 2.2 * body_size
                        and rect_w(l["bbox"]) < 0.5 * W]
                if len(near) >= 2 or area(r) > 0.02 * W * H:
                    fig_rects.append(union([r] + [l["bbox"] for l in near]))
        # merge overlapping / touching figure rects (panels of one figure)
        merged = []
        for r in sorted(fig_rects, key=lambda r: r[1]):
            for m in merged:
                if inter_area(grow(r, 10), m) > 0:
                    m[:] = union([m, r])
                    break
            else:
                merged.append(list(r))
        fig_rects = [r for r in merged if area(r) > 0.004 * W * H]
        flow, side = [], []
        for l in lines:
            if l["furniture"]:
                l["role"] = l["zone"]
                side.append(l)
            elif l["rotated"]:
                l["role"] = "margin"
                side.append(l)
            elif in_box(l, tbl_boxes):
                l["role"] = "table_text"
            elif in_box(l, fig_rects, 0.8) and not CAPTION_RE.match(l["text"]):
                l["role"] = "figure_text"
            else:
                l["role"] = "body"
                flow.append(l)
        fig_text = [l for l in lines if l.get("role") == "figure_text"]
        for fr in fig_rects:
            figures_out.append({"id": "fig-p%d-%d" % (i + 1, len(figures_out) + 1), "page": i + 1, "bbox": r2(fr),
                                "inner_text": [l["text"] for l in fig_text if inter_area(l["bbox"], fr) > 0][:60]})

        # layout blocks -> reading order
        blks = make_blocks(merge_rows(flow))
        ordered = xy_order(blks, W)
        # column label
        gp = vertical_groups(blks, max(6.0, 0.012 * W)) if blks else None
        ncols = len(gp) if gp else 1
        page_infos.append({"page": i + 1, "width": round(W, 1), "height": round(H, 1), "kind": pg["kind"],
                           "columns": ncols, "ocr": pg["ocr"], "ocr_note": pg["ocr_note"], "n_lines": len(lines),
                           "n_figures": len(fig_rects), "n_tables": len(pg["tables"])})
        # paragraphs
        ctx = {"body": body_size, "height": H, "ocr": pg["ocr"]}
        order_no = 0
        page_items = []
        for blk in ordered:
            col = "full" if rect_w(blk["bbox"]) >= 0.62 * W else None
            for para in split_paragraphs(blk, body_size):
                info = classify_paragraph(para, ctx)
                text, nh = block_text(para)
                marked = None
                if any(l.get("marked") for l in para):
                    marked, _ = block_text(para, "marked")
                bbox = union([l["bbox"] for l in para])
                if col is None and gp:
                    cx = (bbox[0] + bbox[2]) / 2
                    col = 0
                    for gi, g in enumerate(gp):
                        gx0 = min(it["bbox"][0] for it in g)
                        gx1 = max(it["bbox"][2] for it in g)
                        if gx0 - 2 <= cx <= gx1 + 2:
                            col = gi
                            break
                page_items.append({"info": info, "text": text, "marked": marked, "bbox": bbox, "lines": para,
                                   "dehyph": nh, "col": col if col is not None else 0,
                                   "ocr_conf": (round(statistics.mean([l["ocr_conf"] for l in para if l.get("ocr_conf") is not None]), 1)
                                                if any(l.get("ocr_conf") is not None for l in para) else None)})
        # tables as blocks (positioned by y among the full-width/column flow)
        for t in pg["tables"]:
            page_items.append({"info": {"type": "table", "flags": []}, "text": "", "marked": None, "bbox": list(t["bbox"]),
                               "lines": [], "dehyph": 0, "col": "full" if rect_w(t["bbox"]) >= 0.62 * W else 0, "table": t,
                               "ocr_conf": None})
        # insert tables at the position of the first body item that starts below them within the same column
        flow_items = [it for it in page_items if "table" not in it["info"].get("type", "")]
        tab_items = [it for it in page_items if it["info"].get("type") == "table"]
        final = []
        pending = sorted(tab_items, key=lambda t: (t["bbox"][1]))
        for it in flow_items:
            for t in list(pending):
                same_col = t["col"] == "full" or it["col"] == "full" or t["col"] == it["col"]
                if same_col and t["bbox"][1] < it["bbox"][1] - 1 and (t["col"] == it["col"] or t["col"] == "full" or it["col"] == "full"):
                    final.append(t)
                    pending.remove(t)
            final.append(it)
        final.extend(pending)
        # side furniture
        for l in side:
            final.append({"info": {"type": l["role"], "flags": []}, "text": l["text"], "marked": None, "bbox": l["bbox"],
                          "lines": [l], "dehyph": 0, "col": "margin", "ocr_conf": None, "side": True})
        for it in final:
            bid += 1
            it["id"] = "p%d-b%03d" % (i + 1, bid)
            it["page"] = i + 1
            it["order"] = bid
        page_infos[-1]["items"] = final
        order_no += len(final)

    # --- front matter: title (possibly wrapped over several blocks) + authors/affiliations before the first real heading
    flat0 = [it for pi in page_infos for it in pi["items"] if not it.get("side")]
    first_real = None
    for k, it in enumerate(flat0):
        if it["info"]["type"] == "heading":
            num, _t = parse_number(it["text"])
            if KEY_HEAD_RE.match(it["text"]) or num:
                first_real = k
                break
    title_blocks = []
    if first_real is not None and flat0 and flat0[0]["page"] == 1:
        pre = [it for it in flat0[:first_real] if it["page"] == 1]
        heads = [it for it in pre if it["info"]["type"] == "heading" and it["info"].get("size")]
        if heads:
            mx = max(h["info"]["size"] for h in heads)
            title_blocks = [h for h in heads if h["info"]["size"] >= mx - 0.3 and h["bbox"][1] < 0.4 * page_infos[0]["height"]]
        for it in pre:
            if it not in title_blocks and it["info"]["type"] in ("heading", "paragraph", "list_item"):
                it["info"]["type"] = "front_matter"
    elif first_real is None:
        heads = [it for it in flat0 if it["page"] == 1 and it["info"]["type"] == "heading" and it["info"].get("size")]
        if heads:
            mx = max(h["info"]["size"] for h in heads)
            title_blocks = [h for h in heads if h["info"]["size"] >= mx - 0.3 and h["bbox"][1] < 0.4 * page_infos[0]["height"]]
    if title_blocks:
        t0 = title_blocks[0]
        t0["info"]["type"], t0["info"]["level"] = "title", 0
        for extra in title_blocks[1:]:
            t0["text"] = (t0["text"] + " " + extra["text"]).strip()
            t0["bbox"] = union([t0["bbox"], extra["bbox"]])
            t0["lines"] = t0["lines"] + extra["lines"]
            extra["merged_into"] = t0["id"]
        t0.setdefault("flags_extra", []).append("title_merged_blocks:%d" % len(title_blocks) if len(title_blocks) > 1 else "title")

    # no explicit "Abstract" heading (APS/Nature style): the long front-matter paragraph is the abstract
    if not any(re.match(r"^\s*(abstract|summary|摘要)\b", it["text"], re.I) and it["info"]["type"] == "heading" for it in flat0):
        fm = [it for it in flat0 if it["info"]["type"] == "front_matter" and it["page"] == 1 and len(it["text"]) >= 300]
        if fm:
            max(fm, key=lambda it: len(it["text"]))["info"]["type"] = "abstract"

    # --- section structure (levels from numbering, else from font-size rank)
    head_sizes = sorted({round(it["info"].get("size", 0), 1) for it in flat0
                         if it["info"]["type"] == "heading" and it["info"].get("size")}, reverse=True)
    sections, cur_sec = [], None
    ref_mode = False
    ref_level = 9
    for it in flat0:
        t = it["info"]["type"]
        if it.get("merged_into"):
            continue
        it["section"] = cur_sec["id"] if cur_sec else None
        if t == "heading":
            num, title = parse_number(it["text"])
            if num and re.match(r"^\d", num):
                level = num.count(".") + 1
            elif num:
                level = num.count(".") + 1
            else:
                sz = round(it["info"].get("size", body_size), 1)
                level = (head_sizes.index(sz) + 1) if sz in head_sizes else 2
            if it["info"].get("level") is None:
                it["info"]["level"] = level
            it["number"], it["title"] = num, title
            sec = {"id": "s%d" % (len(sections) + 1), "title": title, "number": num, "level": it["info"]["level"],
                   "block": it["id"], "page": it["page"]}
            sections.append(sec)
            cur_sec = sec
            it["section"] = sec["id"]
            is_ref = bool(REF_HEAD_RE.match(it["text"])) or ("references_heading" in it["info"]["flags"])
            if is_ref:
                ref_mode, ref_level = True, it["info"]["level"]
            elif ref_mode and it["info"]["level"] <= ref_level:
                ref_mode, ref_level = False, 9  # e.g. "Appendix" after the references
        elif ref_mode and t in ("paragraph", "list_item"):
            it["info"]["type"] = "reference_block"
    # title fallback: largest-size block on page 1 before any section
    if not title_blocks and page_infos:
        cand = [it for it in flat0 if it["page"] == 1 and it["info"]["type"] in ("front_matter", "paragraph") and it["lines"]]
        if cand:
            best = max(cand, key=lambda it: statistics.mean([l["size"] for l in it["lines"]]))
            if statistics.mean([l["size"] for l in best["lines"]]) > body_size + 2:
                best["info"]["type"], best["info"]["level"] = "title", 0

    # --- cross column / cross page paragraph continuation (geometry: next block starts higher on the page, or on a later page)
    flat = [it for pi in page_infos for it in pi["items"] if not it.get("side") and not it.get("merged_into")]
    merged_count = 0
    prev = None
    for it in flat:
        t = it["info"]["type"]
        if prev is not None and t == "paragraph" and prev["info"]["type"] in ("paragraph", "caption") and it["text"]:
            ptxt = prev["text"].rstrip()
            starts_lower = it["text"][:1].islower() or it["text"][:1] in "(,;)"
            Hn = pi_height(page_infos, it["page"])
            col_break = it["page"] != prev["page"] or it["bbox"][1] < prev["bbox"][1] - 5
            same_sec = prev.get("section") == it.get("section")
            if prev["info"]["type"] == "paragraph":
                ok = col_break and same_sec and (it["page"] != prev["page"] or it["bbox"][1] < 0.5 * Hn)
            else:  # caption wrapped into a following block of the same (small) font size
                psz = statistics.mean([l["size"] for l in prev["lines"]]) if prev["lines"] else 0
                isz = statistics.mean([l["size"] for l in it["lines"]]) if it["lines"] else 99
                ok = abs(psz - isz) < 0.4 and it["page"] == prev["page"] and abs(it["bbox"][0] - prev["bbox"][0]) < 6 \
                    and 0 <= it["bbox"][1] - prev["bbox"][3] < 18
            if ptxt and not ptxt.endswith(TERMINAL) and starts_lower and ok:
                prev["text"], dh = block_text([{"text": ptxt}, {"text": it["text"]}])
                prev["dehyph"] = prev.get("dehyph", 0) + dh
                prev["bbox"] = union([prev["bbox"], it["bbox"]]) if it["page"] == prev["page"] and prev["info"]["type"] == "caption" else prev["bbox"]
                prev.setdefault("pages", [prev["page"]])
                if it["page"] not in prev["pages"]:
                    prev["pages"].append(it["page"])
                prev.setdefault("continuation_bboxes", []).append({"page": it["page"], "bbox": r2(it["bbox"]), "block": it["id"]})
                it["merged_into"] = prev["id"]
                merged_count += 1
                continue
        if t not in ("header", "footer", "margin"):
            prev = it

    # --- reference entries
    references = []
    ref_blocks = [it for it in flat if it["info"]["type"] == "reference_block" and not it.get("merged_into")]
    if ref_blocks:
        for it in ref_blocks:
            lines = it["lines"]
            col_min = min(l["bbox"][0] for l in lines)
            entries, curr = [], []
            for l in lines:
                starts = bool(re.match(r"^\s*(\[\d{1,3}\]|\d{1,3}\.\s|\(\d{1,3}\)\s)", l["text"]))
                hang = bool(curr) and l["bbox"][0] <= col_min + 1.5 and curr[-1]["bbox"][0] > col_min + 1.5
                if curr and (starts or hang):
                    entries.append(curr)
                    curr = [l]
                else:
                    curr.append(l)
            if curr:
                entries.append(curr)
            # the block itself might be a single hanging-indent entry per block (common) -> keep as is
            for e in entries:
                txt, _ = block_text(e)
                m = re.match(r"^\s*(\[\d{1,3}\]|\d{1,3}\.|\(\d{1,3}\))\s*", txt)
                references.append({"id": "ref%d" % (len(references) + 1), "label": m.group(1) if m else None,
                                   "text": txt[m.end():] if m else txt, "page": it["page"], "bbox": r2(union([l["bbox"] for l in e])),
                                   "block": it["id"]})
    # --- tables: cross-page merge, markdown/html
    tables_out = []
    tab_items = [it for it in flat if it["info"]["type"] == "table"]
    page_height = {pi["page"]: pi["height"] for pi in page_infos}
    for it in tab_items:
        t = it["table"]
        rows = fill_merged(t["rows"])
        t_out = {"id": "tbl%d" % (len(tables_out) + 1), "block": it["id"], "pages": [it["page"]], "bbox": r2(t["bbox"]),
                 "n_rows": len(rows), "n_cols": max(len(r) for r in rows), "rows": rows, "strategy": t["strategy"],
                 "caption": None, "merged_from": [], "flags": []}
        # merged-cell detection
        gi = grid_from_cells(t.get("cells"), t["bbox"])
        if gi:
            nr, nc, grid = gi
            if len(grid) != nr * nc:
                t_out["flags"].append("has_merged_cells")
        t_out["_gi"] = gi
        tables_out.append(t_out)
        it["table_ref"] = t_out["id"]
    # cross-page continuation
    for a, b in zip(tables_out, tables_out[1:]):
        pa, pb = a["pages"][-1], b["pages"][0]
        if pb == pa + 1 and a["bbox"][3] > 0.78 * page_height[pa] and b["bbox"][1] < 0.22 * page_height[pb] and \
                a["n_cols"] == b["n_cols"] and abs(a["bbox"][0] - b["bbox"][0]) < 0.04 * 612 + 6:
            hdr_same = b["rows"] and a["rows"] and [c.strip().lower() for c in b["rows"][0]] == [c.strip().lower() for c in a["rows"][0]]
            a["rows"].extend(b["rows"][1:] if hdr_same else b["rows"])
            a["pages"].append(pb)
            a["n_rows"] = len(a["rows"])
            a["merged_from"].append(b["id"])
            a["flags"].append("merged_across_pages" + ("_repeated_header_dropped" if hdr_same else ""))
            b["_dropped"] = True
    tables_out = [t for t in tables_out if not t.get("_dropped")]
    # caption link: nearest caption block above/below on the same page
    caps = [it for it in flat if it["info"]["type"] == "caption" and not it.get("merged_into")]
    for t in tables_out:
        best, bd = None, 1e9
        for c in caps:
            if c["page"] != t["pages"][0] or not re.match(r"^\s*(Table|TABLE|表)", c["text"]):
                continue
            d = min(abs(c["bbox"][3] - t["bbox"][1]), abs(c["bbox"][1] - t["bbox"][3]))
            if d < bd and d < 80:
                best, bd = c, d
        if best:
            t["caption"] = {"block": best["id"], "text": best["text"]}
    for f in figures_out:
        best, bd = None, 1e9
        for c in caps:
            if c["page"] != f["page"] or not re.match(r"^\s*(Figure|Fig|FIGURE|图|Scheme|Chart)", c["text"]):
                continue
            d = min(abs(c["bbox"][1] - f["bbox"][3]), abs(c["bbox"][3] - f["bbox"][1]))
            if d < bd and d < 90:
                best, bd = c, d
        f["caption"] = {"block": best["id"], "text": best["text"]} if best else None

    # --- assemble output blocks
    final_blocks = []
    formulas = []
    for pi in page_infos:
        for it in pi["items"]:
            if it.get("merged_into"):
                continue
            t = it["info"]["type"]
            blk = {"id": it["id"], "type": t, "page": it["page"], "bbox": r2(it["bbox"]),
                   "column": it["col"], "order": it["order"], "text": it["text"]}
            if it.get("pages") and len(it["pages"]) > 1:
                blk["pages"] = it["pages"]
                blk["continuation"] = it.get("continuation_bboxes")
            if it.get("marked"):
                blk["text_marked"] = it["marked"]
            if t == "heading" or t == "title":
                blk["level"] = it["info"].get("level")
                if it.get("number"):
                    blk["number"] = it["number"]
            if it.get("section"):
                blk["section"] = it["section"]
            if it["info"].get("flags"):
                blk["flags"] = list(it["info"]["flags"])
            for fx in it.get("flags_extra", []):
                if fx != "title":
                    blk.setdefault("flags", []).append(fx)
            if it.get("dehyph"):
                blk.setdefault("flags", []).append("dehyphenated:%d" % it["dehyph"])
            if it.get("ocr_conf") is not None:
                blk["ocr_confidence"] = it["ocr_conf"]
                if it["ocr_conf"] < 60:
                    blk.setdefault("flags", []).append("low_ocr_confidence")
            if t == "table":
                tref = next((x for x in tables_out if x["id"] == it.get("table_ref")), None)
                if tref is None:
                    continue
                blk["table_id"] = tref["id"]
                blk["text"] = ""
            if t == "formula":
                m = EQ_NO_RE.search(it["text"])
                formulas.append({"id": "eq%d" % (len(formulas) + 1), "block": it["id"], "page": it["page"], "bbox": r2(it["bbox"]),
                                 "number": m.group(1) if m else None, "text": it["text"], "text_marked": it.get("marked"),
                                 "note": "linearised from text layer; sub/superscripts shown in text_marked; not LaTeX-verified"})
            final_blocks.append(blk)

    for t in tables_out:
        gi = t.pop("_gi", None)
        t["markdown"] = table_markdown(t["rows"])
        if gi and "has_merged_cells" in t["flags"] and len(t["pages"]) == 1:
            try:
                words = be.doc[t["pages"][0] - 1].get_text("words") if be.name == "pymupdf" else []
                t["html"] = table_html(t["rows"], gi, words)
            except Exception:
                t["html"] = None
        t["confidence"] = 0.8 if t["strategy"] == "lines" and not t["flags"] else 0.6

    # furniture summary
    furn = defaultdict(list)
    for b in final_blocks:
        if b["type"] in ("header", "footer", "margin"):
            furn[b["type"] + "s"].append({"page": b["page"], "text": b["text"]})

    # --- status
    needs = [u for u in unparsed if u["reason"] == "needs_ocr"]
    n_text_pages = sum(1 for p in page_infos if p["kind"] == "text" or p["ocr"])
    if n_text_pages == 0:
        status = "failed" if needs else "partial"
    elif needs or any(u["reason"] == "extraction_error" for u in unparsed) or be.name == "pypdf":
        status = "partial"
    else:
        status = "success"
    if be.name == "pypdf":
        warnings.append("degraded backend (pypdf): no geometry, reading order/columns/tables are NOT verified")
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    sections_out = [s for s in sections]
    doc = {
        "schema": SCHEMA, "tool_version": VERSION,
        "source": {"file": os.path.basename(path), "sha256": sha, "pages": n_pages, "backend": be.name,
                   "metadata": be.metadata(), "body_font_size": body_size},
        "status": status,
        "pages": [{k: v for k, v in pi.items() if k != "items"} for pi in page_infos],
        "blocks": final_blocks, "sections": sections_out, "tables": tables_out, "formulas": formulas,
        "references": references, "figures": figures_out, "furniture": dict(furn),
        "unparsed": unparsed, "warnings": warnings,
        "stats": {"blocks": len(final_blocks), "paragraphs_merged_across_column_or_page": merged_count,
                  "headings": len(sections_out), "tables": len(tables_out), "formulas": len(formulas),
                  "references": len(references), "figures": len(figures_out)},
    }
    return doc


def pi_height(page_infos, page_no):
    for p in page_infos:
        if p["page"] == page_no:
            return p["height"]
    return 792.0


# ----------------------------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------------------------
def render_markdown(doc):
    out = ["<!-- scholarly-pdf-parser %s | source: %s | backend: %s | status: %s -->" % (
        doc["tool_version"], doc["source"]["file"], doc["source"]["backend"], doc["status"]), ""]
    tmap = {t["id"]: t for t in doc["tables"]}
    for b in doc["blocks"]:
        t = b["type"]
        anchor = "<!-- %s p%s -->" % (b["id"], ",".join(str(p) for p in b.get("pages", [b["page"]])))
        if t in ("header", "footer", "margin"):
            continue
        if t == "title":
            out += [anchor, "# " + b["text"], ""]
        elif t == "heading":
            lvl = min(6, max(1, (b.get("level") or 2) + 1))
            out += [anchor, "#" * lvl + " " + b["text"], ""]
        elif t == "table":
            tb = tmap.get(b.get("table_id"))
            if tb:
                cap = (tb.get("caption") or {}).get("text")
                out += [anchor, "**" + cap + "**" if cap else "", tb.get("html") or tb["markdown"], ""]
        elif t == "caption":
            out += [anchor, "*" + b["text"] + "*", ""]
        elif t == "formula":
            out += [anchor, "```math", b.get("text_marked") or b["text"], "```", ""]
        elif t == "footnote":
            out += [anchor, "> [footnote] " + b["text"], ""]
        elif t == "list_item":
            out += [anchor, "- " + b["text"], ""]
        elif t == "reference_block":
            continue
        else:
            out += [anchor, b["text"], ""]
    if doc["references"]:
        out += ["## References (parsed entries)", ""]
        for r in doc["references"]:
            out.append("- <!-- %s p%d --> %s %s" % (r["id"], r["page"], r["label"] or "", r["text"]))
        out.append("")
    return "\n".join(x for x in out if x is not None)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse a scientific PDF into structured, evidence-linked JSON/Markdown.")
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="auto", choices=["auto", "pymupdf", "pdfplumber", "pypdf"])
    ap.add_argument("--ocr", default="auto", choices=["auto", "off", "force"])
    ap.add_argument("--lang", default="eng", help="tesseract language, e.g. eng, chi_sim, eng+chi_sim")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--ocr-engine", default="auto", choices=["auto", "rapidocr", "tesseract", "pytesseract"], help="OCR engine preference")
    ap.add_argument("--no-tables", action="store_true")
    ap.add_argument("--ocr-budget", type=float, default=420.0, help="max seconds spent on OCR over the whole document")
    a = ap.parse_args(argv)
    global OCR_ENGINE_PREF
    OCR_ENGINE_PREF = a.ocr_engine
    os.makedirs(a.out, exist_ok=True)
    if not os.path.isfile(a.pdf):
        print(json.dumps({"status": "failed", "error": "input file not found: " + a.pdf}))
        return 2
    with open(a.pdf, "rb") as fh:
        head = fh.read(1024)
    if b"%PDF" not in head:
        print(json.dumps({"status": "failed", "error": "not a PDF (missing %PDF header)"}))
        return 2
    try:
        doc = parse_pdf(a.pdf, a.out, a.backend, a.ocr, a.lang, a.max_pages, not a.no_tables, a.ocr_budget)
    except PermissionError as e:
        res = {"status": "failed", "error": str(e), "hint": "encrypted PDF: ask the user for the password; do not guess"}
        json.dump(res, open(os.path.join(a.out, "quality_report.json"), "w"), ensure_ascii=False, indent=2)
        print(json.dumps(res))
        return 3
    except Exception as e:
        res = {"status": "failed", "error": "%s: %s" % (type(e).__name__, str(e)[:300])}
        json.dump(res, open(os.path.join(a.out, "quality_report.json"), "w"), ensure_ascii=False, indent=2)
        print(json.dumps(res))
        return 1
    with open(os.path.join(a.out, "parsed.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "parsed.md"), "w", encoding="utf-8") as fh:
        fh.write(render_markdown(doc))
    rep = {"status": doc["status"], "backend": doc["source"]["backend"], "pages": doc["source"]["pages"],
           "stats": doc["stats"], "warnings": doc["warnings"], "unparsed": doc["unparsed"],
           "page_kinds": dict(Counter(p["kind"] for p in doc["pages"])),
           "columns_per_page": [p["columns"] for p in doc["pages"]]}
    with open(os.path.join(a.out, "quality_report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"status": doc["status"], "out": a.out, "pages": doc["source"]["pages"], "stats": doc["stats"],
                      "warnings": len(doc["warnings"]), "unparsed": len(doc["unparsed"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
