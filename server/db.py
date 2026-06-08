import sqlite3
from typing import List, Optional, Dict, Any
import json
import threading
from pathlib import Path

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
                    limit_requests INTEGER DEFAULT -1,
                    remaining_requests INTEGER DEFAULT -1,
                    limit_tokens INTEGER DEFAULT -1,
                    remaining_tokens INTEGER DEFAULT -1,
                    reset_requests TEXT,
                    last_heartbeat_at TEXT,
                    is_healthy BOOLEAN DEFAULT 1
                )
            """)
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

def pick_best_ready_account() -> Optional[sqlite3.Row]:
    """Returns the ready account with the most remaining requests."""
    with db_lock:
        with get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM accounts 
                WHERE status = 'READY' AND is_healthy = 1 
                ORDER BY remaining_requests DESC 
                LIMIT 1
            """)
            return cursor.fetchone()

# Initialize DB on import
init_db()
