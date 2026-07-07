import sqlite3
import os
os.makedirs('data', exist_ok=True)
db_path = os.path.join('data', 'audit_state.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS voucher_ledger (
    file_hash TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    voucher_id TEXT,
    vendor_name TEXT,
    warrant_total REAL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'PROCESSING', 'VALIDATED', 'EXCEPTION')),
    error_message TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
""")
conn.commit()
conn.close()
print("Database file built successfully!")
