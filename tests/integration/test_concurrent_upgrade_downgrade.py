"""Concurrent `upgrade head` and `downgrade -1` on the same environment (#31).

One real process upgrades while another downgrades, starting from r1 applied and r2 pending. The project lock
serializes them, so either order is valid; a typed `MigrationError` from one side is tolerated as fail-closed.
Whatever happens, the ledger is a prefix of source history with no duplicates and `prompts` equals its replay.
Coordination uses multiprocessing events, never sleeps. Child targets are module-level so they pickle under spawn.
"""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from promptmigrate import core, revisions, store
from promptmigrate.errors import MigrationError

from pmtest import write_revision

R1, R2 = "000000000001", "000000000002"
ENV = "dev"
_TIMEOUT = 30


class _BarrierTimeout(TimeoutError):
    """A child's start barrier timed out: a test-infrastructure failure, never a valid outcome."""


def _build_project(project: Path) -> Path:
    """Two revisions in history, r1 applied to `dev`, r2 pending."""
    write_revision(project, R1, None, {"assistant": {"before": None, "after": {"m": 1}}})
    write_revision(project, R2, R1, {"assistant": {"before": {"m": 1}, "after": {"m": 2}}})
    assert core.upgrade(project, ENV, R1) == [R1]
    return project


def _child(action: str, project_dir: Path, ready, go, queue) -> None:
    """Signal ready, wait for the barrier, run `upgrade head` or `downgrade -1`, report (action, ids, exc type)."""
    ready.set()
    if not go.wait(_TIMEOUT):
        queue.put((action, [], _BarrierTimeout))
        return
    ids: list[str] = []
    exc_type: type[BaseException] | None = None
    try:
        ids = core.upgrade(project_dir, ENV) if action == "upgrade" else core.downgrade(project_dir, ENV, "-1")
    except BaseException as exc:  # reported to the parent, which decides whether the type is allowed
        exc_type = type(exc)
    queue.put((action, ids, exc_type))


def _state(project_dir: Path) -> tuple[dict, list[str]]:
    with store.connect_readonly(project_dir, ENV) as db:
        assert db is not None, "the environment database must exist"
        rows = {name: json.loads(config) for name, config in db.execute("SELECT name, config FROM prompts")}
        ledger = [row[0] for row in db.execute("SELECT revision FROM applied_revisions ORDER BY seq")]
    return rows, ledger


def test_concurrent_upgrade_and_downgrade_leave_a_consistent_state(project):
    project_dir = _build_project(project)
    history = [r.revision for r in revisions.load_revisions(project_dir)]
    ctx = multiprocessing.get_context()
    ready = {"upgrade": ctx.Event(), "downgrade": ctx.Event()}
    go = ctx.Event()
    queue: multiprocessing.Queue = ctx.Queue()
    processes = [ctx.Process(target=_child, args=(action, project_dir, ready[action], go, queue))
                 for action in ("upgrade", "downgrade")]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(_TIMEOUT) for event in ready.values()), "both children must reach the start barrier"
        go.set()
        # Read results before join: a child blocks on exit until its queue data is consumed.
        results = {action: (ids, exc) for action, ids, exc in (queue.get(timeout=_TIMEOUT * 2) for _ in processes)}
        for process in processes:
            process.join(_TIMEOUT)
        assert not any(process.is_alive() for process in processes), "children must not hang"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()

    assert set(results) == {"upgrade", "downgrade"}, results
    up_ids, up_exc = results["upgrade"]
    down_ids, down_exc = results["downgrade"]
    for exc in (up_exc, down_exc):
        assert exc is None or issubclass(exc, MigrationError), f"untyped failure: {results!r}"
    assert not (up_exc and down_exc), f"both processes failed: {results!r}"

    rows, ledger = _state(project_dir)
    assert len(set(ledger)) == len(ledger), f"duplicate ledger rows: {ledger}"
    assert ledger == history[:len(ledger)], f"ledger is not a prefix of source history: {ledger}"
    assert rows == core.replay_revisions(revisions.load_revisions(project_dir)[:len(ledger)]), (rows, ledger)

    if up_exc is None and down_exc is None:
        # Both ran, serialized: U then D, or D then U. Nothing else is reachable.
        assert (up_ids, down_ids, ledger) in (([R2], [R2], [R1]), ([R1, R2], [R1], [R1, R2])), results
