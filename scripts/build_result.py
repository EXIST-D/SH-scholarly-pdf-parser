#!/usr/bin/env python3
"""build_result.py - build / validate /app/output/result.json from the case contract and a parse.

The evaluator gives /input/case.json with an `output_contract`. result.json must contain EVERY contract field plus
`case_id`, and should carry honest status, sources, uncertainty and change records. This script:
  * reads the contract in any common shape (list of names, list of {name,...}, {name: spec}, {fields:[...]}),
  * fills what can be derived truthfully from parsed.json (sources, status, uncertainty, change records, tables, ...),
  * takes everything the agent decided (answers, evidence) from --answers answers.json,
  * leaves unknown fields as null and LISTS them -- it never invents values,
  * with --validate, checks an existing result.json against the contract.

usage:
  python3 build_result.py --case /input/case.json --parsed /app/work/parsed --out /app/output/result.json \
         [--answers /app/work/answers.json] [--check /app/work/check.json] [--status partial --reason "..."]
  python3 build_result.py --case /input/case.json --validate /app/output/result.json
exit codes: 0 complete | 2 written but some contract fields unfilled | 3 validation failed | 1 error
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def load_json(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def find_key(obj, names):
    """Depth-first search for the first key in `names`."""
    if isinstance(obj, dict):
        for k in names:
            if k in obj:
                return obj[k]
        for v in obj.values():
            r = find_key(v, names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, names)
            if r is not None:
                return r
    return None


def contract_fields(case):
    oc = find_key(case, ["output_contract", "outputContract", "output_schema", "outputs"])
    fields = []

    def add(x):
        if isinstance(x, str) and x not in fields:
            fields.append(x)

    if oc is None:
        return fields, None
    if isinstance(oc, list):
        for it in oc:
            if isinstance(it, str):
                add(it)
            elif isinstance(it, dict):
                add(it.get("name") or it.get("field") or it.get("key") or it.get("id"))
    elif isinstance(oc, dict):
        inner = oc.get("fields") or oc.get("required") or oc.get("properties") or oc.get("required_fields")
        if isinstance(inner, list):
            for it in inner:
                add(it if isinstance(it, str) else (it.get("name") or it.get("field") or it.get("key")) if isinstance(it, dict) else None)
        elif isinstance(inner, dict):
            for k in inner:
                add(k)
        else:
            for k, v in oc.items():
                if k not in ("type", "description", "title", "version", "schema", "$schema", "additionalProperties"):
                    add(k)
    return fields, oc


def derive(name, parsed, check, status, reason):
    """Values that can be derived truthfully. Returns (found, value)."""
    n = name.lower()
    src = parsed.get("source", {}) if parsed else {}
    if n in ("status", "run_status", "state", "outcome"):
        return True, status
    if n in ("sources", "source", "input", "inputs", "input_id", "input_identification", "input_identifier", "inputs_used", "input_files"):
        return True, [{"file": src.get("file"), "sha256": src.get("sha256"), "pages": src.get("pages"),
                       "parser": "scholarly-pdf-parser", "backend": src.get("backend")}]
    if n in ("uncertainty", "uncertainties", "limitations", "limits", "unresolved", "caveats", "uncertain_items", "risks"):
        items = []
        for w in (parsed or {}).get("warnings", []):
            items.append({"kind": "warning", "detail": w})
        for u in (parsed or {}).get("unparsed", []):
            items.append({"kind": "unparsed", "page": u.get("page"), "detail": "%s %s" % (u.get("reason"), u.get("detail", "")).strip()})
        if check:
            for i in check.get("issues", []):
                if i.get("severity") in ("warn", "fail"):
                    items.append({"kind": "check:" + i["check"], "page": i.get("page"), "detail": i["detail"]})
        if reason:
            items.append({"kind": "run_note", "detail": reason})
        return True, items
    if n in ("change_records", "changes", "change_log", "change_record", "modifications", "edits", "revisions"):
        st = (parsed or {}).get("stats", {})
        recs = [{"change": "paragraphs merged across column/page breaks", "count": st.get("paragraphs_merged_across_column_or_page", 0)}]
        recs += [{"change": "table rebuilt from word positions", "table": t["id"], "detail": t.get("note", "")}
                 for t in (parsed or {}).get("tables", []) if t.get("strategy") == "words"]
        recs += [{"change": "table merged across pages", "table": t["id"], "pages": t["pages"]}
                 for t in (parsed or {}).get("tables", []) if t.get("merged_from")]
        dh = [(b["id"], b["page"], f) for b in (parsed or {}).get("blocks", []) for f in (b.get("flags") or []) if str(f).startswith("dehyphenated:")]
        if dh:
            recs.append({"change": "line-end soft hyphens rejoined (original kept hyphen when the compound occurs hyphenated elsewhere in the document)",
                         "count": sum(int(f.split(":")[1]) for _, _, f in dh), "blocks": [b for b, _, _ in dh][:25]})
        for w in (parsed or {}).get("warnings", []):
            if w.startswith("glyph repair"):
                recs.append({"change": "glyph repair", "detail": w})
        return True, recs
    if not parsed:
        return False, None
    table = {"sections": parsed.get("sections"), "headings": parsed.get("sections"), "tables": parsed.get("tables"),
             "formulas": parsed.get("formulas"), "equations": parsed.get("formulas"), "references": parsed.get("references"),
             "bibliography": parsed.get("references"), "figures": parsed.get("figures"), "blocks": parsed.get("blocks"),
             "pages": parsed.get("pages"), "furniture": parsed.get("furniture")}
    if n in table and table[n] is not None:
        return True, table[n]
    return False, None


def validate(case, result):
    fields, _ = contract_fields(case)
    problems = []
    cid = find_key(case, ["case_id", "caseId", "id"])
    if cid is not None and result.get("case_id") != cid:
        problems.append("case_id missing or different (expected %r)" % cid)
    if "case_id" not in result:
        problems.append("case_id missing")
    for f in fields:
        if f not in result:
            problems.append("contract field missing: " + f)
        elif result[f] is None:
            problems.append("contract field is null: " + f)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--parsed")
    ap.add_argument("--out")
    ap.add_argument("--answers")
    ap.add_argument("--check")
    ap.add_argument("--status", choices=["success", "partial", "failed", "undeterminable", "refused"])
    ap.add_argument("--reason")
    ap.add_argument("--validate")
    a = ap.parse_args(argv)
    try:
        case = load_json(a.case)
    except Exception as e:
        print(json.dumps({"error": "cannot read case file: %s" % e}))
        return 1
    if a.validate:
        try:
            res = load_json(a.validate)
        except Exception as e:
            print(json.dumps({"valid": False, "problems": ["result.json is not valid JSON: %s" % e]}))
            return 3
        pr = validate(case, res)
        print(json.dumps({"valid": not pr, "problems": pr}, ensure_ascii=False))
        return 0 if not pr else 3
    if not a.out:
        ap.error("--out is required unless --validate is used")
    parsed = None
    if a.parsed:
        pj = a.parsed if a.parsed.endswith(".json") else os.path.join(a.parsed, "parsed.json")
        if os.path.isfile(pj):
            parsed = load_json(pj)
    check = load_json(a.check) if a.check and os.path.isfile(a.check) else None
    answers = load_json(a.answers) if a.answers and os.path.isfile(a.answers) else {}
    status = a.status or (parsed or {}).get("status") or "failed"
    fields, _ = contract_fields(case)
    cid = find_key(case, ["case_id", "caseId"]) or find_key(case, ["id"])
    result = {"case_id": cid}
    unfilled = []
    for f in fields:
        if f == "case_id":
            if f in answers:
                result[f] = answers[f]
            continue
        if f in answers:
            result[f] = answers[f]
            continue
        ok, v = derive(f, parsed, check, status, a.reason)
        if ok:
            result[f] = v
        else:
            result[f] = None
            unfilled.append(f)
    for extra, nm in (("status", "status"), ("sources", "sources"), ("uncertainty", "uncertainty"), ("change_records", "change_records")):
        if extra not in result:
            result[extra] = answers.get(extra, derive(nm, parsed, check, status, a.reason)[1])
    for k, v in answers.items():
        result.setdefault(k, v)
    if cid is None:
        result["case_id"] = None
        unfilled.append("case_id")
    result["_meta"] = {"generator": "scholarly-pdf-parser/build_result.py", "contract_fields": fields,
                       "unfilled_fields": unfilled, "contract_found": bool(fields)}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    msg = {"written": a.out, "case_id": result["case_id"], "contract_fields": fields, "unfilled_fields": unfilled,
           "status": result.get("status")}
    if not fields:
        msg["warning"] = "no output_contract found in the case file: read /input/case.json and task.md and add the fields by hand"
    print(json.dumps(msg, ensure_ascii=False))
    return 2 if (unfilled or not fields) else 0


if __name__ == "__main__":
    sys.exit(main())
