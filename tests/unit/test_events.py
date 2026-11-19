"""ADR-029: the events table schema, its append-only triggers and the shape check (store/events.py)."""
import sqlite3

import pytest

from promptmigrate.errors import StorageError
from promptmigrate.store import events


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


def rows(db):
    return db.execute("SELECT command, revision, checksum, previous_checksum FROM events ORDER BY seq").fetchall()


def test_ensure_creates_the_table_with_exactly_the_documented_columns(db):
    events.ensure(db)
    assert tuple(row[1] for row in db.execute("PRAGMA table_info(events)")) == events.COLUMNS


def test_ensure_is_idempotent(db):
    events.ensure(db)
    events.record(db, "upgrade", "a" * 12, "c" * 64)
    events.ensure(db)
    assert rows(db) == [("upgrade", "a" * 12, "c" * 64, None)]


def test_record_stores_command_ids_checksums_and_a_utc_timestamp(db):
    events.ensure(db)
    events.record(db, "restamp", "a" * 12, "n" * 64, "o" * 64)
    assert rows(db) == [("restamp", "a" * 12, "n" * 64, "o" * 64)]
    stamp = db.execute("SELECT occurred_at FROM events").fetchone()[0]
    assert stamp.endswith("+00:00")


def test_update_is_rejected(db):
    events.ensure(db)
    events.record(db, "upgrade", "a" * 12, "c" * 64)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE events SET revision='b'")
    assert rows(db) == [("upgrade", "a" * 12, "c" * 64, None)]


def test_delete_is_rejected(db):
    events.ensure(db)
    events.record(db, "upgrade", "a" * 12, "c" * 64)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM events")
    assert len(rows(db)) == 1


def test_unknown_command_is_rejected(db):
    events.ensure(db)
    with pytest.raises(sqlite3.IntegrityError):
        events.record(db, "drop", "a" * 12, "c" * 64)


def test_a_table_with_other_columns_fails_closed(db):
    db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, config TEXT)")
    with pytest.raises(StorageError, match="events"):
        events.ensure(db)


def test_ensure_and_record_never_commit(db):
    db.execute("BEGIN")
    events.ensure(db)
    events.record(db, "upgrade", "a" * 12, "c" * 64)
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='events'").fetchone()[0] == 0
