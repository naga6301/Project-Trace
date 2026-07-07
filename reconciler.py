"""
reconciler.py - Phase 3: detect fields, run the Triple-Lock, write a verdict.

Detection (local key-value extraction): each field's value is found by anchor
label + spatial proximity + a value pattern - coordinate-free, so it tolerates
re-flowed / split layouts. No cloud model (charter: air-gapped).

Triple-Lock:
  Lock 1 - Identity+amount : voucher, object code and amount all matched in master.
  Lock 2 - Summation       : PDF warrant total == sum of the master's line amounts.
  Lock 3 - Integrity       : vendor matches, amount present, date agrees (or absent).

Routing:
  identity/confidence problem  -> EXCEPTION  (04_Exceptions)
  locks fail on read-fine data -> REVIEW     (05_ManualReview)
  all three locks pass         -> VALIDATED  (03_Validated)
"""
import re
import sqlite3

import pandas as pd
from rapidfuzz import fuzz

import config as cfg


def to_number(s):
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s)))
    except (ValueError, TypeError):
        return None


def norm(s):
    return re.sub(r"\s+", "", str(s)).upper().lstrip("0")


def norm_date(s):
    return re.sub(r"\D", "", str(s))   # digits only, for lenient date compare


def load_master():
    df = pd.read_excel(cfg.MASTER_EXCEL, dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]
    return df


def get_spans(conn, file_hash):
    cur = conn.execute("""SELECT page,text,x0,y0,x1,y1,coord_space,dpi,confidence
                          FROM extractions WHERE file_hash = ?""", (file_hash,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def page_widths(spans):
    w = {}
    for s in spans:
        w[s["page"]] = max(w.get(s["page"], 1.0), s["x1"])
    return w


ROW_WEIGHT = 3.0   # vertical distance counts more: a label sits beside its value


def _anchor_distance(span, anchor_spans, widths):
    best = 1e9
    for a in anchor_spans:
        if a["page"] != span["page"]:
            continue
        wd = widths.get(span["page"], 1.0) or 1.0
        dx = ((span["x0"] + span["x1"]) / 2 - (a["x0"] + a["x1"]) / 2) / wd
        dy = ((span["y0"] + span["y1"]) / 2 - (a["y0"] + a["y1"]) / 2) / wd
        # Penalise values to the LEFT of the label (values usually sit to the right).
        left_penalty = 0.15 if dx < 0 else 0.0
        best = min(best, (dx * dx + (ROW_WEIGHT * dy) ** 2) ** 0.5 + left_penalty)
    return best


# Every word that appears in any anchor phrase - a value span made up only of
# label words (e.g. "Warrant Total", "Voucher No.") can't itself be a value.
LABEL_WORDS = set()
for _phrase in set(cfg.TOTAL_ANCHORS).union(
        *[r.get("anchors", []) for r in cfg.FIELD_RULES.values()]):
    LABEL_WORDS.update(_phrase.split())


def _is_pure_label(text):
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and all(w in LABEL_WORDS for w in words)


def find_by_anchors(spans, pattern, anchors, widths):
    rx = re.compile(pattern, re.I)
    # A value span can't be a field label (e.g. the word "Vendor" isn't the vendor).
    cands = [s for s in spans if rx.search(s["text"]) and not _is_pure_label(s["text"])]
    if not cands:
        return None
    anchor_spans = [s for s in spans if any(a in s["text"].lower() for a in anchors)] if anchors else []
    best = sorted(cands, key=lambda s: (_anchor_distance(s, anchor_spans, widths), -s["confidence"]))[0]
    best = dict(best)
    best["value"] = rx.search(best["text"]).group()
    best["anchored"] = bool(anchor_spans) and _anchor_distance(best, anchor_spans, widths) <= cfg.PROXIMITY_LIMIT
    return best


def detect_all(spans, widths):
    out = {name: find_by_anchors(spans, r["pattern"], r.get("anchors", []), widths)
           for name, r in cfg.FIELD_RULES.items()}
    out["_total"] = find_by_anchors(spans, cfg.FIELD_RULES["amount"]["pattern"],
                                    cfg.TOTAL_ANCHORS, widths)
    return out


def field_matches(field, det, rows):
    """True if the detected value matches any master row for this voucher."""
    rule = cfg.FIELD_RULES[field]
    vals = rows[cfg.EXCEL_COLUMNS[field]].tolist()
    method = rule["method"]
    if method == "numeric":
        dv = to_number(det["value"])
        return dv is not None and any(
            to_number(v) is not None and abs(to_number(v) - dv) <= cfg.SUMMATION_TOLERANCE for v in vals)
    if method == "exact":
        return norm(det["value"]) in {norm(v) for v in vals}
    if method == "fuzzy":
        return any(fuzz.token_sort_ratio(det["value"], v) >= rule.get("thresh", 85) for v in vals)
    if method == "date":
        d = norm_date(det["value"])
        return any(d and norm_date(v).endswith(d[-4:]) for v in vals)   # lenient
    return False


def evaluate(detected, master):
    """Return (status, reasons, locks, passed_fields)."""
    reasons, passed = [], []
    vc = cfg.EXCEL_COLUMNS["voucher_number"]

    det_v = detected.get("voucher_number")
    if not det_v:
        return "EXCEPTION", ["voucher number not found on PDF"], {}, passed
    if det_v["confidence"] < cfg.OCR_CONFIDENCE_FLOOR:
        return "EXCEPTION", [f"voucher number low confidence ({det_v['confidence']:.2f})"], {}, passed

    rows = master[master[vc].map(norm) == norm(det_v["value"])]
    if rows.empty:
        return "EXCEPTION", [f"voucher {det_v['value']} not found in master"], {}, passed
    passed.append("voucher_number")

    # A low-confidence read on any must-match field is an extraction problem -> EXCEPTION.
    for f, r in cfg.FIELD_RULES.items():
        d = detected.get(f)
        if r.get("must") and d and d["confidence"] < cfg.OCR_CONFIDENCE_FLOOR:
            return "EXCEPTION", [f"{f} low confidence ({d['confidence']:.2f})"], {}, passed

    # ---- Lock 1: identity + amount matched in master ------------------------
    lock1 = True
    for f in ("object_code", "amount"):
        d = detected.get(f)
        if d and field_matches(f, d, rows):
            passed.append(f)
        else:
            lock1 = False
            reasons.append(f"lock1: {f} " + ("mismatch" if d else "not found"))
    # grant is must, but treat a missing grant as a soft identity note
    dg = detected.get("grant_number")
    if dg and field_matches("grant_number", dg, rows):
        passed.append("grant_number")
    elif dg:
        lock1 = False
        reasons.append("lock1: grant_number mismatch")

    # ---- Lock 2: summation (PDF total == sum of master line amounts) --------
    master_total = sum(v for v in (to_number(x) for x in rows[cfg.EXCEL_COLUMNS["amount"]]) if v is not None)
    det_total = detected.get("_total")
    if det_total is not None:
        dt = to_number(det_total["value"])
        lock2 = dt is not None and abs(dt - master_total) <= cfg.SUMMATION_TOLERANCE
    else:
        lock2 = False
    if not lock2:
        reasons.append(f"lock2: total {det_total['value'] if det_total else 'not found'} "
                       f"!= master sum {master_total:.2f}")

    # ---- Lock 3: vendor + date + amount integrity ---------------------------
    lock3 = True
    dv = detected.get("vendor")
    if dv and not field_matches("vendor", dv, rows):
        lock3 = False
        reasons.append("lock3: vendor mismatch")
    elif dv:
        passed.append("vendor")
    dd = detected.get("date")
    if dd and not field_matches("date", dd, rows):
        lock3 = False
        reasons.append("lock3: date mismatch")
    if detected.get("amount") is None:
        lock3 = False
        reasons.append("lock3: amount not found")

    locks = {"lock1": lock1, "lock2": lock2, "lock3": lock3}
    status = "VALIDATED" if all(locks.values()) else "REVIEW"
    return status, ([] if status == "VALIDATED" else reasons), locks, passed


def save(conn, file_hash, detected, passed, status, reasons, locks):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, file_hash TEXT, field TEXT, value TEXT,
            page INTEGER, x0 REAL, y0 REAL, x1 REAL, y1 REAL, coord_space TEXT, dpi INTEGER, verdict TEXT)
    """)
    conn.execute("DELETE FROM matches WHERE file_hash = ?", (file_hash,))
    for f in passed:
        d = detected.get(f)
        if not d:
            continue
        conn.execute("""INSERT INTO matches (file_hash,field,value,page,x0,y0,x1,y1,coord_space,dpi,verdict)
                        VALUES (?,?,?,?,?,?,?,?,?,?, 'pass')""",
                     (file_hash, f, d["value"], d["page"], d["x0"], d["y0"], d["x1"], d["y1"],
                      d["coord_space"], d["dpi"]))
    log = "; ".join(reasons) if reasons else (
        "locks: " + ",".join(k for k, v in locks.items() if v) if locks else None)
    conn.execute("""UPDATE voucher_ledger SET status=?, error_log=?, timestamp=CURRENT_TIMESTAMP
                    WHERE file_hash=?""", (status, log, file_hash))
    conn.commit()


def run():
    master = load_master()
    conn = sqlite3.connect(cfg.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    files = conn.execute(
        "SELECT file_hash, file_name FROM voucher_ledger WHERE status = 'EXTRACTED'").fetchall()
    print(f"[Reconciler] {len(files)} file(s) to reconcile.")

    for file_hash, file_name in files:
        spans = get_spans(conn, file_hash)
        if not spans:
            save(conn, file_hash, {}, [], "EXCEPTION", ["no extracted text"], {})
            print(f"  {file_name}: EXCEPTION (no text)")
            continue
        widths = page_widths(spans)
        detected = detect_all(spans, widths)
        status, reasons, locks, passed = evaluate(detected, master)
        save(conn, file_hash, detected, passed, status, reasons, locks)
        print(f"  {file_name}: {status}" + (f" ({'; '.join(reasons)})" if reasons else ""))
    conn.close()


if __name__ == "__main__":
    run()
