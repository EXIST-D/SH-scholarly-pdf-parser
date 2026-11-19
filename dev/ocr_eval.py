#!/usr/bin/env python3
"""Dev tool: measure OCR quality on SYNTHETIC scans (rasterised pages of public papers) against the original text layer."""
import re, sys, json, time
from collections import Counter
sys.path.insert(0, '../submission/scripts')
import pymupdf
import parse_pdf as pp

def toks(s): return re.findall(r"[a-z0-9]{2,}", s.lower())

def run(orig_pdf, scan_pdf, page_map, dpi):
    o = pymupdf.open(orig_pdf); be = pp.PyMuPDFBackend(scan_pdf)
    tot_ref = tot_hit = tot_hyp = runon = 0; secs = 0.0
    for si, oi in enumerate(page_map):
        ref = Counter(toks(pp.clean_text(o[oi].get_text('text'))))
        t = time.time(); lines, note = pp.ocr_page(be, si, 'eng', dpi=dpi); secs += time.time() - t
        hyp_text = ' '.join(l['text'] for l in (lines or []))
        hyp = Counter(toks(hyp_text))
        hit = sum((ref & hyp).values())
        tot_ref += sum(ref.values()); tot_hit += hit; tot_hyp += sum(hyp.values())
        runon += sum(1 for w in re.findall(r"[A-Za-z]{19,}", hyp_text))
    return dict(dpi=dpi, recall=round(tot_hit / tot_ref, 4), precision=round(tot_hit / max(1, tot_hyp), 4), runon_tokens=runon, sec_per_page=round(secs / len(page_map), 1))

if __name__ == '__main__':
    for name, pm in (('resnet_cvpr_2col', (0, 3)), ('bert_naacl_2col_tables', (0, 5))):
        for dpi in (200, 300):
            print(name, run(f'samples/{name}.pdf', f'ocr/scan_{name}.pdf', pm, dpi))
