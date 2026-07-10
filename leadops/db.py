from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "app.db"


def get_db_path() -> Path:
    return Path(os.environ.get("LEADOPS_DB", DEFAULT_DB_PATH))


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection tuned for safe multi-student concurrent use.

    WAL lets readers and a single writer proceed without blocking each other,
    and busy_timeout makes competing writers wait-and-retry instead of raising
    'database is locked'. This is what makes several students reviewing at once
    safe on the bundled dev server.
    """
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        # Some network/synced filesystems reject WAL; fall back silently.
        pass
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_type TEXT NOT NULL,
    source_file TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_rows INTEGER NOT NULL DEFAULT 0,
    accepted_rows INTEGER NOT NULL DEFAULT 0,
    blocked_invalid INTEGER NOT NULL DEFAULT 0,
    blocked_suppressed INTEGER NOT NULL DEFAULT 0,
    blocked_duplicate INTEGER NOT NULL DEFAULT 0,
    matched_rows INTEGER NOT NULL DEFAULT 0,
    created_messages INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_external_id TEXT,
    business_name TEXT,
    first_name TEXT,
    last_name TEXT,
    phone_original TEXT,
    phone_normalized TEXT,
    city TEXT,
    state TEXT,
    vertical TEXT,
    website TEXT,
    address TEXT,
    source TEXT,
    source_file TEXT,
    source_row INTEGER,
    batch_id INTEGER REFERENCES batches(id),
    assigned_student_id INTEGER REFERENCES students(id),
    status TEXT NOT NULL DEFAULT 'pending',
    block_reason TEXT,
    skip_reason TEXT,
    reply_status TEXT,
    hot INTEGER NOT NULL DEFAULT 0,
    meeting_status TEXT,
    notes TEXT,
    commission_credit REAL NOT NULL DEFAULT 0,
    payout_credit REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone_normalized);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_contacts_student ON contacts(assigned_student_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id),
    original_ai_message TEXT,
    current_message TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_student_id INTEGER REFERENCES students(id),
    approved_by INTEGER REFERENCES students(id),
    approved_at TEXT,
    exported_at TEXT,
    source_file TEXT,
    source_row INTEGER,
    batch_id INTEGER REFERENCES batches(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_student ON messages(assigned_student_id);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id),
    message_id INTEGER REFERENCES messages(id),
    student_id INTEGER REFERENCES students(id),
    reply_status TEXT,
    hot INTEGER NOT NULL DEFAULT 0,
    meeting_status TEXT,
    notes TEXT,
    commission_credit REAL NOT NULL DEFAULT 0,
    payout_credit REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(contact_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_hot ON outcomes(hot);
CREATE INDEX IF NOT EXISTS idx_outcomes_student ON outcomes(student_id);

CREATE TABLE IF NOT EXISTS suppression_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_original TEXT,
    phone_normalized TEXT NOT NULL UNIQUE,
    source_file TEXT,
    source_row INTEGER,
    batch_id INTEGER REFERENCES batches(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_role TEXT,
    actor_id INTEGER,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


DEFAULT_STUDENTS = (
    ("Maya Torres", "maya@example.local"),
    ("Jordan Smith", "jordan@example.local"),
    ("Taylor Nguyen", "taylor@example.local"),
)


def init_db(reset: bool = False) -> Path:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
        # Remove stale WAL/SHM sidecar files so a reset is a true clean slate.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        existing = conn.execute("SELECT COUNT(*) AS count FROM students").fetchone()["count"]
        if existing == 0:
            conn.executemany(
                "INSERT INTO students (name, email) VALUES (?, ?)",
                DEFAULT_STUDENTS,
            )
        conn.commit()
    return db_path


def audit(
    conn: sqlite3.Connection,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: str | None = None,
    actor_role: str | None = None,
    actor_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log
            (actor_role, actor_id, action, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor_role, actor_id, action, entity_type, entity_id, details),
    )
