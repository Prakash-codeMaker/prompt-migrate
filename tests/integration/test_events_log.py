"""ADR-029 (#14): upgrade, downgrade and restamp append events in the ledger's transaction."""
import sqlite3
from contextlib import closing

import pytest

import pmtest
from promptmigrate.core import check, downgrade, load_revisions, upgrade
from promptmigrate.errors import DriftError

A, B = "a" * 12, "b" * 12


@pytest.fixture
def two(project):
    pmtest.write_revision(project, A, None, {"a": pmtest.create({"v": "A1"})}, format_version=1)
    pmtest.write_revision(project, B, A, {"a": {"before": {"v": "A1"}, "after": {"v": "A2"}}}, format_version=1)
    return project


def db_file(project):
    return pmtest.pm_dir(project) / "states" / "dev.sqlite3"


def checksums(project):
    return {r.revision: r.checksum for r in load_revisions(project)}


def make_legacy(project):
    """Make dev look like it was written by a release without the events table."""
    with closing(sqlite3.connect(db_file(project))) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("DROP TRIGGER events_no_delete")
        db.execute("DROP TABLE events")
        db.commit()


def test_upgrade_records_one_event_per_applied_revision(two):
    upgrade(two, "dev")
    sums = checksums(two)
    assert pmtest.read_events(two) == [("upgrade", A, sums[A], None), ("upgrade", B, sums[B], None)]


def test_downgrade_records_events_for_what_the_ledger_loses(two):
    upgrade(two, "dev")
    downgrade(two, "dev", "base")
    sums = checksums(two)
    events = pmtest.read_events(two)
    assert events[2:] == [("downgrade", B, sums[B], None), ("downgrade", A, sums[A], None)]
    assert len(events) == 4
    assert check(two, "dev") == (0, 2)


def test_events_survive_the_downgrade_that_deletes_ledger_rows(two):
    upgrade(two, "dev")
    downgrade(two, "dev", A)
    with closing(sqlite3.connect(db_file(two))) as db:
        assert db.execute("SELECT revision FROM applied_revisions").fetchall() == [(A,)]
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_a_no_op_upgrade_writes_no_event(two):
    upgrade(two, "dev")
    before = pmtest.read_events(two)
    upgrade(two, "dev")
    assert pmtest.read_events(two) == before


def test_a_state_db_without_the_table_is_adopted_on_the_first_mutation(two):
    upgrade(two, "dev", A)
    make_legacy(two)
    assert check(two, "dev") == (1, 2)  # reads work without the table
    assert pmtest.read_events(two) == []
    upgrade(two, "dev")
    assert pmtest.read_events(two) == [("upgrade", B, checksums(two)[B], None)]


def test_a_rejected_command_does_not_adopt_the_table(two):
    upgrade(two, "dev", A)
    make_legacy(two)
    with closing(sqlite3.connect(db_file(two))) as db:
        db.execute("UPDATE prompts SET config='{\"v\":\"tampered\"}'")
        db.commit()
    with pytest.raises(DriftError):
        upgrade(two, "dev")
    with closing(sqlite3.connect(db_file(two))) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone() is None


def test_events_contain_no_config_content(two):
    upgrade(two, "dev")
    flat = " ".join(str(value) for row in pmtest.read_events(two) for value in row)
    assert "A1" not in flat and "A2" not in flat
