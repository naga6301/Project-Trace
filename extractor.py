"""
extractor.py - Phase 2: claim PENDING/RETRY files, extract every text span.

Per file: claim it (-> PROCESSING), read each page (digital text layer first,
OCR fallback for scans), store spans with page/box/coord_space/dpi/confidence,
then flip to EXTRACTED. If the OCR-path mean confidence stays below the floor
even after a fallback preprocessing profile, route the file to EXCEPTION
(the flowchart's "OCR Confidence >=85%?" gate).

paddleocr and cv2 are imported lazily, so this module loads (and the digital
path runs) even where they aren't installed.
"""
import os
import sqlite3
from datetime import datetime

import numpy as np

import config as cfg

DPI = 300
ZOOM = DPI / 72
TEXT_LAYER_MIN_WORDS = 20

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR   # lazy
        _ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    return _ocr


def _preprocess(img, profile):
    """Deskew/denoise on the OCR path. 'fallback' adds binarization. cv2 optional."""
    if not cfg.ENABLE_PREPROCESS:
        return img
    try:
        import cv2
    except Exception:
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    # Deskew via the dominant text angle.
    coords = np.column_stack(np.where(gray < 200))
    if len(coords) > 100:
        angle = cv2.minAreaRect(coords)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if abs(angle) > 0.5:
            h, w = gray.shape
            M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            gray = cv2.warpAffine(gray, M, (w, h),
                                  flags=cv2.INTER_CUBIC, borderValue=255)
    gray = cv2.fastNlMeansDenoising(gray, h=10)
    if profile == "fallback":
        gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, 31, 10)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)


def _digital_words(page):
    spans = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if word.strip():
            spans.append({"text": word, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                          "coord_space": "pdf_points", "source": "digital", "confidence": 1.0})
    return spans


def _ocr_page(page, profile):
    import fitz  # lazy (only needed for rendering)
    pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), colorspace=fitz.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    img = _preprocess(img, profile)
    spans = []
    for line in (_get_ocr().ocr(img, cls=True)[0] or []):
        box, (text, conf) = line
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        if text.strip():
            spans.append({"text": text, "x0": min(xs), "y0": min(ys),
                          "x1": max(xs), "y1": max(ys),
                          "coord_space": "pixels", "source": "ocr", "confidence": float(conf)})
    return spans


def extract(pdf_path, profile="default"):
    """Return (spans, mean_ocr_confidence). Digital pages don't affect OCR confidence."""
    import fitz  # lazy
    doc = fitz.open(pdf_path)
    spans, ocr_confs = [], []
    for i, page in enumerate(doc):
        words = _digital_words(page)
        page_spans = words if len(words) >= TEXT_LAYER_MIN_WORDS else _ocr_page(page, profile)
        for s in page_spans:
            s["page"] = i
            s["dpi"] = DPI
            if s["source"] == "ocr":
                ocr_confs.append(s["confidence"])
        spans.extend(page_spans)
    doc.close()
    mean_conf = float(np.mean(ocr_confs)) if ocr_confs else 1.0   # all-digital -> 1.0
    return spans, mean_conf


def _save(conn, file_hash, spans, ocr_conf, status):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, file_hash TEXT, page INTEGER, text TEXT,
            x0 REAL, y0 REAL, x1 REAL, y1 REAL, coord_space TEXT, dpi INTEGER,
            source TEXT, confidence REAL)
    """)
    conn.execute("DELETE FROM extractions WHERE file_hash = ?", (file_hash,))
    conn.executemany("""
        INSERT INTO extractions (file_hash, page, text, x0, y0, x1, y1, coord_space, dpi, source, confidence)
        VALUES (:fh,:page,:text,:x0,:y0,:x1,:y1,:coord_space,:dpi,:source,:confidence)
    """, [dict(s, fh=file_hash) for s in spans])
    conn.execute("""
        UPDATE voucher_ledger SET status = ?, ocr_confidence = ?, timestamp = CURRENT_TIMESTAMP
        WHERE file_hash = ?
    """, (status, round(ocr_conf, 4), file_hash))
    conn.commit()


def run():
    conn = sqlite3.connect(cfg.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    files = conn.execute(
        "SELECT file_hash, file_name FROM voucher_ledger WHERE status IN ('PENDING','RETRY')"
    ).fetchall()
    print(f"[Extractor] {len(files)} file(s) to extract.")

    for file_hash, file_name in files:
        # Claim atomically so a second worker won't grab the same row.
        claimed = conn.execute("""
            UPDATE voucher_ledger SET status = 'PROCESSING', timestamp = CURRENT_TIMESTAMP
            WHERE file_hash = ? AND status IN ('PENDING','RETRY')
        """, (file_hash,)).rowcount
        conn.commit()
        if not claimed:
            continue

        src = os.path.join(cfg.INBOUND_DIR, file_name)
        if not os.path.exists(src):
            conn.execute("UPDATE voucher_ledger SET status='EXCEPTION', error_log=? WHERE file_hash=?",
                         ("source PDF missing", file_hash))
            conn.commit()
            print(f"  {file_name}: EXCEPTION (source missing)")
            continue

        try:
            spans, conf = extract(src, "default")
            if conf < cfg.OCR_CONFIDENCE_FLOOR:        # retry with heavier preprocessing
                spans, conf = extract(src, "fallback")
            status = "EXTRACTED" if conf >= cfg.OCR_CONFIDENCE_FLOOR else "EXCEPTION"
            _save(conn, file_hash, spans, conf, status)
            note = "" if status == "EXTRACTED" else f" (low OCR confidence {conf:.2f})"
            print(f"  {file_name}: {status} [{len(spans)} spans]{note}")
        except Exception as e:
            conn.execute("UPDATE voucher_ledger SET status='EXCEPTION', error_log=? WHERE file_hash=?",
                         (f"extract error: {e}", file_hash))
            conn.commit()
            print(f"  {file_name}: EXCEPTION ({e})")

    conn.close()


if __name__ == "__main__":
    run()
