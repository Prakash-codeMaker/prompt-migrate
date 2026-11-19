"""Append-only `events` table of an environment state DB (ADR-029): schema, triggers and the row writer.

Callers own the connection and the transaction; nothing here commits.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from ..errors import StorageError

# (name, declared type, NOT NULL flag, primary-key flag) as PRAGMA table_info reports them for _TABLE.
_SHAPE = (
    ("seq", "INTEGER", 0, 1), ("occurred_at", "TEXT", 1, 0), ("command", "TEXT", 1, 0),
    ("revision", "TEXT", 1, 0), ("checksum", "TEXT", 1, 0), ("previous_checksum", "TEXT", 0, 0),
)
COLUMNS = tuple(column[0] for column in _SHAPE)

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
    "CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events "
    "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
    "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END",
    # INSERT OR REPLACE on an existing seq deletes the old row without firing the DELETE trigger (SQLite only does so
    # with recursive_triggers on, a per-connection setting), so an insert that reuses a seq is rejected here.
    "CREATE TRIGGER IF NOT EXISTS events_no_replace BEFORE INSERT ON events "
    "WHEN EXISTS (SELECT 1 FROM events WHERE seq = NEW.seq) "
    "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END",
)


_TRIGGER_NAMES = {"events_no_update", "events_no_delete", "events_no_replace"}


def ensure(db: sqlite3.Connection) -> None:
    """Create the table and its append-only triggers if absent; fail closed on any other shape."""
    try:
        db.execute(_TABLE)
        shape = tuple((row[1], row[2].upper(), row[3], row[5]) for row in db.execute("PRAGMA table_info(events)"))
        if shape != _SHAPE:
            raise StorageError("Invalid environment state: events table has unexpected columns")
        for trigger in _TRIGGERS:  # only on a table of the expected shape
            db.execute(trigger)
        # IF NOT EXISTS skips a trigger name already used on another table, which would leave `events` unguarded.
        bound = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'")}
        if bound != _TRIGGER_NAMES:
            raise StorageError("Invalid environment state: events triggers are missing or bound to another table")
    except sqlite3.Error as exc:  # e.g. a view named `events`, or a same-named trigger on another table
        raise StorageError(f"Invalid environment state: cannot create the events table: {exc}") from exc


def record(db: sqlite3.Connection, command: str, revision: str, checksum: str,
           previous_checksum: str | None = None) -> None:
    """Append one event row; the table must exist (see `ensure`)."""
    db.execute(
        "INSERT INTO events(occurred_at, command, revision, checksum, previous_checksum) VALUES(?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), command, revision, checksum, previous_checksum),
    )
