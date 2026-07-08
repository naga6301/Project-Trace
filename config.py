"""
config.py - the single tuning surface for Project TRACE.

Everything that adapts the pipeline to YOUR documents lives here so the team can
retune without touching engine code. Items marked TUNE are the ones that most
affect accuracy and should be validated against your ground-truth sample.
"""
import os
import glob

# ---- Folders ----------------------------------------------------------------
# One destination per failure type, matching the flowchart + charter framework.
INBOUND_DIR       = "01_Inbound"
PROCESSING_DIR    = "02_Processing"
VALIDATED_DIR     = "03_Validated"
EXCEPTIONS_DIR    = "04_Exceptions"      # OCR / confidence / extraction failures
MANUAL_REVIEW_DIR = "05_ManualReview"    # triple-lock value mismatches
DUPLICATE_DIR     = "06_Duplicates"      # duplicate hashes

DATA_DIR     = "data"
DB_PATH      = os.path.join(DATA_DIR, "audit_state.db")
REPORT_DIR   = os.path.join(DATA_DIR, "reports")
LOG_PATH     = os.path.join(DATA_DIR, "trace.log")
# Master Excel: you don't rename it. find_master() (below) auto-detects whatever
# .xlsx/.xls is in data/, or you pass a path: python reconciler.py path\to\file.xlsx

# Which ledger status lands a file in which folder (used by annotator).
STATUS_FOLDER = {
    "VALIDATED": VALIDATED_DIR,
    "EXCEPTION": EXCEPTIONS_DIR,
    "REVIEW":    MANUAL_REVIEW_DIR,
}

# ---- Excel column mapping ---------------------------------------------------
# Left = internal name. Right = EXACT column header in your FLAIR extract.
EXCEL_COLUMNS = {
    "voucher_number": "Primary Document",
    "grant_number":   "Grant Number",
    "grant_year":     "Grant Year",
    "object_code":    "Object Code",
    "amount":         "Amount Numeric",
    "vendor":         "Vendor Name",
    "description":    "Description",
    "date":           "Transaction Date",
}

# ---- TUNE #1: what makes a voucher LINE unique ------------------------------
LINE_KEY = ["voucher_number", "object_code", "amount"]   # <-- confirm/adjust

# ---- TUNE #2: per-field detection + match rules -----------------------------
# anchors : label text near the value on the PDF (case-insensitive)
# pattern : regex the value should match
# method  : "numeric" | "exact" | "fuzzy" | "date"
# must    : True -> a mismatch fails the record ; False -> corroborating only
# thresh  : minimum RapidFuzz ratio for "fuzzy"
FIELD_RULES = {
    "voucher_number": {"anchors": ["voucher no", "document"],    "pattern": r"\d{6}",
                       "method": "exact",   "must": True},
    "grant_number":   {"anchors": ["grant"],                      "pattern": r"\d{4}[A-Z]?",
                       "method": "exact",   "must": True},
    "grant_year":     {"anchors": ["year"],                       "pattern": r"\d{4}",
                       "method": "exact",   "must": False},
    "object_code":    {"anchors": ["object", "category"],         "pattern": r"\d{6}",
                       "method": "exact",   "must": True},
    "amount":         {"anchors": ["amount", "total", "warrant"], "pattern": r"\d[\d,]*\.\d{2}",
                       "method": "numeric", "must": True},
    "vendor":         {"anchors": ["vendor", "payee"],            "pattern": r".+",
                       "method": "fuzzy",   "must": False, "thresh": 88},
    "description":    {"anchors": ["description", "desc"],         "pattern": r".+",
                       "method": "fuzzy",   "must": False, "thresh": 80},
    "date":           {"anchors": ["date"],                        "pattern": r"\d{1,2}[/\-\s]\w{1,3}[/\-\s]\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4}",
                       "method": "date",    "must": False},
}

# Anchors that mark the voucher-level TOTAL, for the summation lock.
TOTAL_ANCHORS = ["warrant amount", "net amount", "total", "amount due"]

# ---- Thresholds -------------------------------------------------------------
OCR_CONFIDENCE_FLOOR = 0.85   # aligned with charter (<85% -> exceptions)
PROXIMITY_LIMIT      = 0.30   # anchor<->value distance (fraction of page width)
SUMMATION_TOLERANCE  = 0.01   # dollars
STALL_MINUTES        = 10     # PROCESSING older than this -> reset to RETRY
ENABLE_PREPROCESS    = True   # deskew/denoise on the OCR path


# ---- Master Excel resolver --------------------------------------------------
def find_master(explicit=None):
    """
    Decide which Excel is the master - no renaming required.
      1. If a path is given (e.g. on the command line), use it.
      2. Otherwise auto-detect the single .xlsx/.xls sitting in data/.
    Raises a clear error on zero or several matches, so the wrong file is never
    chosen silently. (Excel lock files like ~$export.xlsx are ignored.)
    """
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"Master Excel not found: {explicit}")
        return explicit

    found = (glob.glob(os.path.join(DATA_DIR, "*.xlsx"))
             + glob.glob(os.path.join(DATA_DIR, "*.xls")))
    candidates = [f for f in found if not os.path.basename(f).startswith("~$")]

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(
            f"No Excel master found in '{DATA_DIR}/'. Put your export there, "
            f"or pass its path:  python reconciler.py path\\to\\file.xlsx")
    names = ", ".join(os.path.basename(c) for c in candidates)
    raise ValueError(
        f"Multiple Excel files in '{DATA_DIR}/' ({names}). Pass the one to use:  "
        f"python reconciler.py path\\to\\file.xlsx")