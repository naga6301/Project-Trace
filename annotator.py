"""
annotator.py - Phase 4: draw the red boxes, route the file, extend the audit hash chain.

VALIDATED files get permanent red rectangles drawn into the page content stream
(not movable annotations) plus a timestamp stamp, then land in 03_Validated. Their
output SHA-256 is appended to a tamper-evident hash chain (each row references the
previous output hash). EXCEPTION/REVIEW files are copied, unmarked, to their folder.
"""
import os
import shutil
import sqlite3
import hashlib
from datetime import datetime

import config as cfg


def to_points(box):
    if box["coord_space"] == "pixels":
        f = 72.0 / box["dpi"]
        return (box["x0"] * f, box["y0"] * f, box["x1"] * f, box["y1"] * f)
    return (box["x0"], box["y0"], box["x1"], box["y1"])


def find_source(file_name):
    for folder in (cfg.INBOUND_DIR, cfg.PROCESSING_DIR):
        p = os.path.join(folder, file_name)
        if os.path.exists(p):
            return p
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def append_hash_chain(conn, file_hash, output_hash):
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_chain (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, file_hash TEXT, output_hash TEXT,
        prev_hash TEXT, timestamp TEXT)""")
    prev = conn.execute("SELECT output_hash FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = prev[0] if prev else "GENESIS"
    conn.execute("INSERT INTO audit_chain (file_hash, output_hash, prev_hash, timestamp) VALUES (?,?,?,?)",
                 (file_hash, output_hash, prev_hash, datetime.now().isoformat()))
    conn.commit()


def annotate(src_path, boxes, out_path):
    import fitz  # lazy
    doc = fitz.open(src_path)
    pad = 2
    for b in boxes:
        x0, y0, x1, y1 = to_points(b)
        doc[b["page"]].draw_rect(fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad),
                                 color=(1, 0, 0), width=1.2)
    doc[0].insert_text((36, 24), f"VALIDATED BY TRACE  {datetime.now():%Y-%m-%d %H:%M}",
                       fontsize=7, color=(1, 0, 0))
    doc.save(out_path, deflate=True)
    doc.close()


def run():
    for d in cfg.STATUS_FOLDER.values():
        os.makedirs(d, exist_ok=True)
    os.makedirs(cfg.PROCESSING_DIR, exist_ok=True)

    conn = sqlite3.connect(cfg.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    files = conn.execute("""SELECT file_hash, file_name, status FROM voucher_ledger
                            WHERE status IN ('VALIDATED','EXCEPTION','REVIEW')""").fetchall()
    print(f"[Annotator] {len(files)} file(s) to route.")

    for file_hash, file_name, status in files:
        src = find_source(file_name)
        if not src:
            print(f"  {file_name}: source not found, skipping.")
            continue
        out_path = os.path.join(cfg.STATUS_FOLDER[status], file_name)

        if status == "VALIDATED":
            boxes = [dict(zip(["field", "page", "x0", "y0", "x1", "y1", "coord_space", "dpi"], r))
                     for r in conn.execute("""SELECT field,page,x0,y0,x1,y1,coord_space,dpi
                                              FROM matches WHERE file_hash=? AND verdict='pass'""",
                                           (file_hash,)).fetchall()]
            annotate(src, boxes, out_path)
            append_hash_chain(conn, file_hash, sha256_file(out_path))   # tamper-evident record
        else:
            shutil.copy2(src, out_path)

        if os.path.dirname(src) == cfg.INBOUND_DIR:
            shutil.move(src, os.path.join(cfg.PROCESSING_DIR, file_name))
        print(f"  {file_name}: {status} -> {cfg.STATUS_FOLDER[status]}")
    conn.close()


if __name__ == "__main__":
    run()
