"""
report.py - Phase 5: export the audit trail + metrics dashboard data to CSV.

Writes reconciliation, exceptions, manual-review, matched-fields, and audit-chain
CSVs, plus a validation summary with the straight-through-processing rate. Reports
contain file names and amounts -> keep local (the .gitignore already excludes data/).
"""
import os
import sqlite3
from datetime import datetime

import pandas as pd

import config as cfg


def _dump(conn, query, path):
    try:
        pd.read_sql_query(query, conn).to_csv(path, index=False)
    except Exception as e:
        print(f"  (skipped {os.path.basename(path)}: {e})")


def run():
    os.makedirs(cfg.REPORT_DIR, exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH, timeout=10)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    p = lambda name: os.path.join(cfg.REPORT_DIR, f"{name}_{stamp}.csv")

    ledger = pd.read_sql_query("""SELECT file_name, status, ocr_confidence, match_score,
                                  error_log, timestamp FROM voucher_ledger
                                  ORDER BY status, file_name""", conn)
    ledger.to_csv(p("reconciliation_report"), index=False)
    ledger[ledger["status"] == "EXCEPTION"].to_csv(p("exceptions_report"), index=False)
    ledger[ledger["status"] == "REVIEW"].to_csv(p("manual_review_report"), index=False)
    _dump(conn, "SELECT file_hash, field, value, page, verdict FROM matches", p("matched_fields"))
    _dump(conn, "SELECT seq, file_hash, output_hash, prev_hash, timestamp FROM audit_chain",
          p("audit_chain"))

    counts = ledger["status"].value_counts().to_dict()
    total = len(ledger)
    validated = counts.get("VALIDATED", 0)
    summary = pd.DataFrame([
        {"metric": "total files", "value": total},
        {"metric": "validated", "value": validated},
        {"metric": "manual review", "value": counts.get("REVIEW", 0)},
        {"metric": "exceptions", "value": counts.get("EXCEPTION", 0)},
        {"metric": "STP rate", "value": f"{(validated/total if total else 0):.1%}"},
    ])
    summary.to_csv(p("validation_summary"), index=False)

    print(f"[Report] {total} files | " + " | ".join(f"{k}: {v}" for k, v in counts.items()))
    if total:
        print(f"[Report] straight-through (validated): {validated/total:.1%}")
    print(f"[Report] CSVs -> {cfg.REPORT_DIR}")
    conn.close()


if __name__ == "__main__":
    run()
