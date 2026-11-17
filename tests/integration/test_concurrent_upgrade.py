"""Concurrent `upgrade head` runs on the same environment (#11).

Two real processes both run `upgrade head --env dev` on one project with two pending
revisions. Exactly one applies them; the other either sees nothing to do or fails closed
with a typed storage/lock error. The final `prompts` table equals replay of the applied
ledger prefix, with no duplicate ledger rows. Coordination uses multiprocessing events
(barriers), never sleeps.

The child target is a module-level function so it can be pickled under Windows spawn.
"""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

from promptmigrate import core, revisions, store
from promptmigrate.errors import StorageError

from pmtest import write_revision


# Errors the upgrade path may raise when a second writer contends for the SQLite lock
# or hits a storage conflict. Anything else is a test failure, not a valid outcome.
_ALLOWED_FAIL_CLOSED = (sqlite3.Error, StorageError)


class _BarrierTimeout(TimeoutError):
    """Raised by a child when the start barrier times out (test-infrastructure failure)."""


def _build_project(project: Path) -> Path:
    """Add two pending revisions `r1`, `r2` to an initialized project (unapplied)."""
    write_revision(project, "000000000001", None, {"assistant": {"before": None, "after": {"m": 1}}})
    write_revision(project, "000000000002", "000000000001", {"assistant": {"before": {"m": 1}, "after": {"m": 2}}})
    return project


def _upgrade_child(project_dir: Path, env: str, ready: multiprocessing.Event,
                   go: multiprocessing.Event, queue: multiprocessing.Queue) -> None:
    """Child process: signal ready, wait for the barrier, then run `upgrade head`."""
    ready.set()
    if not go.wait(30):
        # Barrier timeout: infrastructure failure, not a valid competing-upgrade outcome.
        queue.put(([], _BarrierTimeout))
        return
    ids: list[str] = []
    exc_type: type[BaseException] | None = None
    try:
        ids = core.upgrade(project_dir, env)  # success: applied pending revision(s)
    except BaseException as exc:
        exc_type = type(exc)
    queue.put((ids, exc_type))


def _rows(project_dir: Path, env: str) -> dict:
    with store.connect_readonly(project_dir, env) as db:
        if db is None:
            return {}
        return {name: json.loads(config) for name, config in db.execute("SELECT name, config FROM prompts").fetchall()}


def _ledger(project_dir: Path, env: str) -> list[str]:
    with store.connect_readonly(project_dir, env) as db:
        if db is None:
            return []
        return [row[0] for row in db.execute("SELECT revision FROM applied_revisions ORDER BY seq").fetchall()]


def test_concurrent_upgrade_applies_once(project):
    project_dir = _build_project(project)
    expected_ids = [r.revision for r in revisions.load_revisions(project_dir)]

    ready_a, ready_b = multiprocessing.Event(), multiprocessing.Event()
    go = multiprocessing.Event()
    queue: multiprocessing.Queue = multiprocessing.Queue()

    processes = [
        multiprocessing.Process(target=_upgrade_child, args=(project_dir, "dev", ready_a, go, queue)),
        multiprocessing.Process(target=_upgrade_child, args=(project_dir, "dev", ready_b, go, queue)),
    ]
    try:
        for process in processes:
            process.start()
        assert ready_a.wait(30) and ready_b.wait(30), "both children must reach the start barrier"
        go.set()
        for process in processes:
            process.join(30)
        assert not any(process.is_alive() for process in processes), "children must not hang"

        results = []
        while not queue.empty():
            results.append(queue.get())
        assert len(results) == 2, f"expected two results, got {results!r}"

        # Exactly one child applies both pending revisions; the other either sees nothing to
        # do (success with empty ids) or fails closed with a permitted typed error.
        applied_two = [(ids, exc) for ids, exc in results if exc is None and ids == expected_ids]
        nothing_to_do = [(ids, exc) for ids, exc in results if exc is None and not ids]
        failed_closed = [(ids, exc) for ids, exc in results
                         if exc is not None and issubclass(exc, _ALLOWED_FAIL_CLOSED)]
        unexpected = [r for r in results if r not in applied_two + nothing_to_do + failed_closed]
        assert not unexpected, f"unexpected child outcome (timeout or untyped error): {results!r}"
        assert len(applied_two) == 1, f"expected exactly one to apply both, got {results!r}"
        assert len(nothing_to_do) + len(failed_closed) == 1, \
            f"expected the other to do nothing or fail closed, got {results!r}"

        expected = core.replay_revisions(revisions.load_revisions(project_dir))
        assert _rows(project_dir, "dev") == expected, (_rows(project_dir, "dev"), expected)
        ledger = _ledger(project_dir, "dev")
        assert ledger == expected_ids, ledger
        assert len(set(ledger)) == len(ledger), f"duplicate ledger rows: {ledger}"
    finally:
        # Clean up any still-running child processes so the suite cannot hang on failure,
        # then close the queue.
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()
