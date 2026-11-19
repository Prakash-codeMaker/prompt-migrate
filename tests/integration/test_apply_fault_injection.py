"""Fault injection (#12): a failure at any write or commit point of upgrade/downgrade leaves the env wholly old.

Every point reachable from `apply.migrate` (and the `_verify` reads before it) is listed in COUNTS. A recording run
proves the table matches what the code really does, and an AST guard fails when `migrate` gains a call the table
does not cover. Faults are installed on the defining modules (`apply`, `revisions`, `sqlite3`), never `core` or `cli`.
"""
import ast
import inspect
import json
import sqlite3
import sys
import textwrap
from collections import Counter
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest

from promptmigrate import apply, revisions
from promptmigrate.core import check, downgrade, upgrade
from promptmigrate.errors import StorageError

import pmtest

REAL_CONNECT = sqlite3.connect  # captured before any patch, so assertions read the real file
A, B, C = "a" * 12, "b" * 12, "c" * 12
SQL_KINDS = {
    "ledger_read": "SELECT revision, checksum FROM applied_revisions",
    "prompts_read": "SELECT name, config FROM prompts",
    "prompts_upsert": "INSERT INTO prompts",
    "prompts_delete": "DELETE FROM prompts",
    "ledger_insert": "INSERT INTO applied_revisions",
    "ledger_delete": "DELETE FROM applied_revisions",
    "events_ddl": "CREATE ",
    "event_insert": "INSERT INTO events",
}
WRITE_KINDS = {"prompts_upsert", "prompts_delete", "ledger_insert", "ledger_delete"}
FUNCTION_KINDS = {"canonical": "canonical", "_apply_changes": "apply_changes", "now": "ledger_timestamp"}

# How many times each point fires in one run, from prefix [A] up to head [A, B, C] and from head back to [A].
COUNTS = {
    "up": {"ledger_read": 1, "prompts_read": 2, "apply_changes": 2, "canonical": 3, "prompts_upsert": 3,
           "prompts_delete": 1, "ledger_timestamp": 2, "ledger_insert": 2, "events_ddl": 4, "event_insert": 2,
           "commit": 1},
    "down": {"ledger_read": 1, "prompts_read": 2, "apply_changes": 2, "canonical": 3, "prompts_upsert": 3,
             "prompts_delete": 1, "ledger_delete": 2, "events_ddl": 4, "event_insert": 2, "commit": 1},
}
CASES = [(direction, kind, n) for direction, kinds in COUNTS.items() for kind, count in kinds.items()
         for n in range(1, count + 1)]


def classify(sql):
    normalized = " ".join(sql.split())
    return next((kind for kind, prefix in SQL_KINDS.items() if normalized.startswith(prefix)), None)


class FaultyConnection:
    """Proxy around a real connection: sqlite3.Connection is a C type, so its execute/commit cannot be patched."""

    def __init__(self, real, injector):
        self._real, self._injector, self.closed = real, injector, False

    def execute(self, sql, *args):
        if " ".join(sql.split()).upper().startswith("BEGIN IMMEDIATE"):
            self._injector.armed = True  # the setup commits before the migration transaction are not under test
        kind = classify(sql)
        if kind:
            self._injector.hit(kind)
        return self._real.execute(sql, *args)

    def commit(self):
        self._injector.hit("commit")
        return self._real.commit()

    def close(self):
        self.closed = True
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class Injector:
    """Counts every injection point and raises at the chosen (kind, occurrence); fault=None only records."""

    def __init__(self, fault=None):
        self.fault, self.counts, self.armed, self.connections = fault, Counter(), False, []

    def hit(self, kind):
        if not self.armed:
            return
        self.counts[kind] += 1
        if self.fault == (kind, self.counts[kind]):
            error = sqlite3.OperationalError if kind in SQL_KINDS or kind == "commit" else RuntimeError
            raise error(f"injected fault at {kind} #{self.counts[kind]}")

    def install(self, m):
        def connect(*args, **kwargs):
            connection = FaultyConnection(REAL_CONNECT(*args, **kwargs), self)
            self.connections.append(connection)
            return connection

        real_canonical, real_apply_changes = apply.canonical, revisions._apply_changes

        def canonical(obj):
            self.hit("canonical")
            return real_canonical(obj)

        def apply_changes(*args, **kwargs):
            # _verify replays history through the same function; only the calls made by migrate are write points.
            if sys._getframe(1).f_code.co_name == "migrate":
                self.hit("apply_changes")
            return real_apply_changes(*args, **kwargs)

        def now(tz):
            self.hit("ledger_timestamp")
            return datetime.now(tz)

        m.setattr(sqlite3, "connect", connect)
        m.setattr(apply, "canonical", canonical)
        m.setattr(apply, "datetime", SimpleNamespace(now=now))
        m.setattr(revisions, "_apply_changes", apply_changes)


@pytest.fixture
def scenario(project):
    """A 3-revision history (B touches three prompts, with a delete) and `dev` applied up to A."""
    pmtest.write_revision(project, A, None, {
        "a": pmtest.create({"v": "A1"}), "b": pmtest.create({"v": "B1"})})
    pmtest.write_revision(project, B, A, {
        "a": {"before": {"v": "A1"}, "after": {"v": "A2"}},
        "b": {"before": {"v": "B1"}, "after": None},
        "c": pmtest.create({"v": "C1"})})
    pmtest.write_revision(project, C, B, {"c": {"before": {"v": "C1"}, "after": {"v": "C2"}}})
    upgrade(project, "dev", A)
    return project


def prepare(project, direction):
    """Move `dev` to the starting point and return (action, applied before, applied after a clean run)."""
    if direction == "up":
        return (lambda: upgrade(project, "dev")), 1, 3
    upgrade(project, "dev")
    return (lambda: downgrade(project, "dev", A)), 3, 1


def raw_state(project):
    """(ledger rows, prompt rows) read straight from the env file, bypassing every command."""
    path = project / ".promptmigrate" / "states" / "dev.sqlite3"
    with closing(REAL_CONNECT(path)) as db:
        ledger = db.execute("SELECT revision, checksum FROM applied_revisions ORDER BY seq").fetchall()
        prompts = {name: json.loads(config) for name, config in db.execute("SELECT name, config FROM prompts")}
    return ledger, prompts


def raw_events(project):
    """Rows of the env's events table read straight from the file; [] when the table does not exist."""
    path = project / ".promptmigrate" / "states" / "dev.sqlite3"
    with closing(REAL_CONNECT(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
            return []
        return db.execute("SELECT command, revision, checksum FROM events ORDER BY seq").fetchall()


def caused_by_injection(exc):
    while exc is not None:
        if "injected fault" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


@pytest.mark.parametrize("direction", sorted(COUNTS))
def test_recorded_injection_points_match_the_table(scenario, direction):
    action, _, after = prepare(scenario, direction)
    injector = Injector()
    with pytest.MonkeyPatch.context() as m:
        injector.install(m)
        action()
    assert injector.counts == COUNTS[direction]
    assert check(scenario, "dev") == (after, 3)


@pytest.mark.parametrize("direction, kind, n", CASES, ids=[f"{d}-{k}-{n}" for d, k, n in CASES])
def test_fault_leaves_environment_wholly_old(scenario, direction, kind, n):
    action, before, after = prepare(scenario, direction)
    ledger_before, prompts_before = raw_state(scenario)
    events_before = raw_events(scenario)
    injector = Injector((kind, n))
    with pytest.MonkeyPatch.context() as m:
        injector.install(m)
        with pytest.raises(Exception) as raised:
            action()
    assert caused_by_injection(raised.value)
    assert injector.counts[kind] == n  # the fault fired at the chosen point, not earlier or later
    assert all(connection.closed for connection in injector.connections)

    ledger, prompts = raw_state(scenario)
    assert ledger == ledger_before
    assert prompts == prompts_before
    assert raw_events(scenario) == events_before  # an event never outlives a rolled-back ledger change, nor the reverse
    assert prompts == revisions.replay_revisions(revisions.load_revisions(scenario)[:before])
    assert check(scenario, "dev") == (before, 3)  # no drift, no integrity error

    action()  # the env is not wedged: the same command succeeds once the fault is gone
    assert len(raw_events(scenario)) == len(events_before) + abs(after - before)
    assert check(scenario, "dev") == (after, 3)
    assert raw_state(scenario)[1] == revisions.replay_revisions(revisions.load_revisions(scenario)[:after])
    scenario.joinpath(".promptmigrate", "states", "dev.sqlite3").unlink()  # PermissionError on Windows if a handle leaked


def test_a_fault_while_adopting_an_old_db_leaves_it_without_the_table(scenario):
    path = scenario / ".promptmigrate" / "states" / "dev.sqlite3"
    with closing(REAL_CONNECT(path)) as db:  # make dev look like it was written by a release without events
        db.execute("DROP TRIGGER events_no_update")
        db.execute("DROP TRIGGER events_no_delete")
        db.execute("DROP TABLE events")
        db.commit()
    state_before = raw_state(scenario)
    injector = Injector(("event_insert", 1))
    with pytest.MonkeyPatch.context() as m:
        injector.install(m)
        with pytest.raises(Exception) as raised:
            upgrade(scenario, "dev")
    assert caused_by_injection(raised.value)
    with closing(REAL_CONNECT(path)) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone() is None  # DDL rolled back
    assert raw_state(scenario) == state_before
    upgrade(scenario, "dev")
    assert len(raw_events(scenario)) == 2


TYPED_READS = [(direction, kind, n) for direction in sorted(COUNTS)
               for kind, n in (("ledger_read", 1), ("prompts_read", 1), ("prompts_read", 2))]


@pytest.mark.parametrize("direction, kind, n", TYPED_READS, ids=[f"{d}-{k}-{n}" for d, k, n in TYPED_READS])
def test_read_fault_raises_a_typed_storage_error(scenario, direction, kind, n):
    action, _, _ = prepare(scenario, direction)
    injector = Injector((kind, n))
    with pytest.MonkeyPatch.context() as m:
        injector.install(m)
        with pytest.raises(StorageError) as raised:
            action()
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_every_call_in_migrate_is_a_declared_injection_point():
    tree = ast.parse(textwrap.dedent(inspect.getsource(apply.migrate)))
    names, sql = set(), set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
        names.add(name)
        if name == "execute":
            sql.add(classify(call.args[0].value))
    assert sql == WRITE_KINDS, "migrate runs SQL that no injection point covers; add it to SQL_KINDS and COUNTS"
    covered = set(FUNCTION_KINDS) | {"execute", "items", "isoformat", "_stored_state", "ensure", "record"}
    assert names <= covered, f"migrate calls {sorted(names - covered)}; add injection points for them"
    assert set(FUNCTION_KINDS.values()) <= {kind for kinds in COUNTS.values() for kind in kinds}
