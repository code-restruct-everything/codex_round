import sqlite3
from typing import List, Optional, Dict, Any
import threading
from pathlib import Path
import logging

logger = logging.getLogger("vault.db")

DB_PATH = Path(__file__).parent / "data.db"
# Ensure the data directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Use a threading lock to ensure thread safety when doing transaction-like operations
db_lock = threading.RLock()

def get_connection():
    # SQLite requires check_same_thread=False for FastAPI if using simple global connections,
    # but we can just create a new connection per function since SQLite is fast.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'READY',
                    checked_out_by TEXT,
                    checked_out_at TEXT,
                    returned_at TEXT,
                    reset_at TEXT,
                    limit_pct INTEGER DEFAULT -1,
                    remaining_pct INTEGER DEFAULT -1,
                    limit_tokens INTEGER DEFAULT -1,
                    remaining_tokens INTEGER DEFAULT -1,
                    reset_requests TEXT,
                    five_hour_percent_left REAL DEFAULT -1,
                    five_hour_reset_at TEXT,
                    weekly_percent_left REAL DEFAULT -1,
                    weekly_reset_at TEXT,
                    usage_updated_at TEXT,
                    usage_source TEXT,
                    usage_error TEXT,
                    plan_type TEXT,
                    rate_limit_reached BOOLEAN DEFAULT 0,
                    last_heartbeat_at TEXT,
                    is_healthy BOOLEAN DEFAULT 1,
                    checkout_request_id TEXT,
                    checkout_acknowledged_at TEXT
                )
            """)
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
            }
            add_columns = {
                "five_hour_percent_left": "REAL DEFAULT -1",
                "five_hour_reset_at": "TEXT",
                "weekly_percent_left": "REAL DEFAULT -1",
                "weekly_reset_at": "TEXT",
                "usage_updated_at": "TEXT",
                "usage_source": "TEXT",
                "usage_error": "TEXT",
                "plan_type": "TEXT",
                "rate_limit_reached": "BOOLEAN DEFAULT 0",
                "limit_pct": "INTEGER DEFAULT -1",
                "remaining_pct": "INTEGER DEFAULT -1",
                "checkout_request_id": "TEXT",
                "checkout_acknowledged_at": "TEXT",
            }
            for column, definition in add_columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
            # Rename legacy columns if they still exist under the old names.
            rename_columns = {"limit_requests": "limit_pct", "remaining_requests": "remaining_pct"}
            for old, new in rename_columns.items():
                if old in existing and new not in existing:
                    conn.execute(f"ALTER TABLE accounts RENAME COLUMN {old} TO {new}")
            conn.commit()

def get_account(account_id: str) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        cursor = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,))
        return cursor.fetchone()

def list_accounts() -> List[sqlite3.Row]:
    with get_connection() as conn:
        cursor = conn.execute("SELECT * FROM accounts")
        return cursor.fetchall()

def insert_account(account_id: str):
    with db_lock:
        with get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO accounts (account_id, status, is_healthy)
                VALUES (?, 'READY', 1)
            """, (account_id,))
            conn.commit()
            logger.debug(f"Inserted or ignored account: {account_id}")

def delete_account(account_id: str):
    with db_lock:
        with get_connection() as conn:
            conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
            conn.commit()

def update_account(account_id: str, updates: Dict[str, Any]):
    if not updates:
        return
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values())
    values.append(account_id)
    with db_lock:
        with get_connection() as conn:
            conn.execute(f"UPDATE accounts SET {set_clause} WHERE account_id = ?", values)
            conn.commit()
            logger.debug(f"Updated account {account_id}: {updates}")

def pick_best_ready_account() -> Optional[sqlite3.Row]:
    """Returns the ready account with the most remaining requests."""
    with db_lock:
        with get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM accounts 
                WHERE status = 'READY' AND is_healthy = 1 
                ORDER BY remaining_pct DESC
                LIMIT 1
            """)
            return cursor.fetchone()

# Initialize DB on import
init_db()
