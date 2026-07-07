"""
watcher.py - Phase 1: ingestion / registry + crash recovery for Project TRACE.

- On startup: recovers files stranded in PROCESSING (crash recovery -> RETRY) and
  scans the inbound folder for files dropped before launch.
- On each new/moved PDF: fingerprints it (SHA-256, after the copy settles),
  registers it as PENDING, or routes a duplicate hash to the duplicate queue.

Registers only; the extractor claims PENDING/RETRY files and processes them.
"""
import os
import time
import shutil
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import config as cfg

os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.FileHandler(cfg.LOG_PATH, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("trace.watcher")


def _connect():
    conn = sqlite3.connect(cfg.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def compute_hash(file_path, retries=5):
    """SHA-256 after the file stops growing; handles Windows locks. None on failure."""
    last_size = -1
    for attempt in range(1, retries + 1):
        try:
            size = os.path.getsize(file_path)
            if size != last_size:                 # still copying
                last_size = size
                time.sleep(attempt * 0.5)
                continue
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
            return h.hexdigest()
        except PermissionError:
            log.warning("Locked, retry %d/%d: %s", attempt, retries, os.path.basename(file_path))
            time.sleep(attempt * 0.5)
        except FileNotFoundError:
            return None
        except Exception as e:
            log.error("Read error on %s: %s", os.path.basename(file_path), e)
            return None
    return None


def recover_stalled():
    """Crash recovery: PROCESSING rows older than STALL_MINUTES -> RETRY."""
    cutoff = (datetime.now() - timedelta(minutes=cfg.STALL_MINUTES)).isoformat()
    conn = _connect()
    cur = conn.execute("""
        UPDATE voucher_ledger SET status = 'RETRY'
        WHERE status = 'PROCESSING' AND timestamp < ?
    """, (cutoff,))
    conn.commit()
    if cur.rowcount:
        log.info("Crash recovery: reset %d stalled file(s) to RETRY.", cur.rowcount)
    conn.close()


def register_file(file_path):
    if os.path.isdir(file_path) or not file_path.lower().endswith(".pdf"):
        return
    file_name = os.path.basename(file_path)
    log.info("Detected: %s", file_name)   # NOTE: file_name may hold a payee name (PII) -> keep log local

    file_hash = compute_hash(file_path)
    if not file_hash:
        log.error("Abandoning %s: unreadable after retries.", file_name)
        return
    log.info("SHA-256: %s...%s", file_hash[:10], file_hash[-10:])

    try:
        conn = _connect()
        row = conn.execute("SELECT status FROM voucher_ledger WHERE file_hash = ?",
                           (file_hash,)).fetchone()
        if row:
            # Duplicate hash -> move the incoming copy to the duplicate queue.
            log.info("Duplicate of a file already '%s'; routing to duplicate queue.", row[0])
            os.makedirs(cfg.DUPLICATE_DIR, exist_ok=True)
            try:
                shutil.move(file_path, os.path.join(cfg.DUPLICATE_DIR, file_name))
            except Exception as e:
                log.error("Could not move duplicate %s: %s", file_name, e)
        else:
            conn.execute("""
                INSERT OR IGNORE INTO voucher_ledger (file_hash, file_name, status, timestamp)
                VALUES (?, ?, 'PENDING', CURRENT_TIMESTAMP)
            """, (file_hash, file_name))
            conn.commit()
            log.info("Registered as PENDING.")
        conn.close()
    except Exception as e:
        log.error("DB error registering %s: %s", file_name, e)


def scan_existing():
    if not os.path.isdir(cfg.INBOUND_DIR):
        return
    existing = [f for f in os.listdir(cfg.INBOUND_DIR) if f.lower().endswith(".pdf")]
    if existing:
        log.info("Startup scan: %d file(s) already in '%s'.", len(existing), cfg.INBOUND_DIR)
        for f in existing:
            register_file(os.path.join(cfg.INBOUND_DIR, f))


class VoucherHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            register_file(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            register_file(event.dest_path)


if __name__ == "__main__":
    os.makedirs(cfg.INBOUND_DIR, exist_ok=True)
    log.info("=" * 52)
    log.info("Project TRACE - Watcher live. Monitoring '%s' (Ctrl+C to stop)", cfg.INBOUND_DIR)
    log.info("=" * 52)

    recover_stalled()   # reclaim anything a previous crash left mid-flight
    scan_existing()     # catch files dropped before launch

    observer = Observer()
    observer.schedule(VoucherHandler(), path=cfg.INBOUND_DIR, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping watcher...")
        observer.stop()
    observer.join()