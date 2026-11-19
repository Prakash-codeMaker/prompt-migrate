"""Append-only `events` table of an environment state DB (ADR-029): schema, triggers and the row writer.

Callers own the connection and the transaction; nothing here commits.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from ..errors import StorageError

COLUMNS = ("seq", "occurred_at", "command", "revision", "checksum", "previous_checksum")

_TABLE = """
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        command TEXT NOT NULL CHECK (command IN ('upgrade', 'downgrade', 'restamp')),
        revision TEXT NOT NULL,
        checksum TEXT NOT NULL,
        previous_checksum TEXT
    )
"""
_TRIGGERS = (
    *(
        f"CREATE TRIGGER IF NOT EXISTS events_no_{verb.lower()} BEFORE {verb} ON events "
        "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END"
        for verb in ("UPDATE", "DELETE")
    ),
    # INSERT OR REPLACE on an existing seq deletes the old row without firing the DELETE trigger (SQLite only does so
    # with recursive_triggers on, a per-connection setting), so an insert that reuses a seq is rejected here.
    "CREATE TRIGGER IF NOT EXISTS events_no_replace BEFORE INSERT ON events "
    "WHEN EXISTS (SELECT 1 FROM events WHERE seq = NEW.seq) "
    "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END",
)


def ensure(db: sqlite3.Connection) -> None:
    """Create the table and its append-only triggers if absent; fail closed on a table with other columns."""
    db.execute(_TABLE)
    for trigger in _TRIGGERS:
        db.execute(trigger)
    columns = tuple(row[1] for row in db.execute("PRAGMA table_info(events)"))
    if columns != COLUMNS:
        raise StorageError("Invalid environment state: events table has unexpected columns")


def record(db: sqlite3.Connection, command: str, revision: str, checksum: str,
           previous_checksum: str | None = None) -> None:
    """Append one event row; the table must exist (see `ensure`)."""
    db.execute(
        "INSERT INTO events(occurred_at, command, revision, checksum, previous_checksum) VALUES(?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), command, revision, checksum, previous_checksum),
    )
