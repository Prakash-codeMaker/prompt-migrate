"""Concurrent `revision` calls on the same source head (#32).

Two real processes run `revision` on one project with the same drafts and the same head. `revision` holds the
project lock around load, diff and write, so the calls serialize: exactly one creates the new revision, and the
other re-reads history under the lock, finds drafts equal to the new head and fails closed with `SchemaError`.
Nothing is overwritten or partial and the history still loads. A `StorageError` is not tolerated: the lock waits
indefinitely, so one would mean a real race bug.
Coordination uses multiprocessing events, never sleeps. The child target is module-level so it pickles under spawn.
"""
from __future__ import annotations

import multiprocessing
from pathlib import Path

from promptmigrate import core
from promptmigrate.errors import SchemaError

from pmtest import CFG, create, pm_dir, set_draft, write_revision

A = "a" * 12
EDIT = {"messages": [{"role": "system", "content": "B"}]}
_TIMEOUT = 30


class _BarrierTimeout(TimeoutError):
    """A child's start barrier timed out: a test-infrastructure failure, never a valid outcome."""


def _child(label: str, project_dir: Path, ready, go, queue) -> None:
    """Signal ready, wait for the barrier, run `revision`, report (label, revision id, exception type, message)."""
    ready.set()
    if not go.wait(_TIMEOUT):
        queue.put((label, None, _BarrierTimeout, ""))
        return
    rid, exc_type, text = None, None, ""
    try:
        rid = core.revision(project_dir, f"edit {label}").revision
    except BaseException as exc:  # reported to the parent, which decides whether the type is allowed
        exc_type, text = type(exc), str(exc)
    queue.put((label, rid, exc_type, text))


def test_concurrent_revision_calls_create_exactly_one_revision(project):
    write_revision(project, A, None, {"assistant": create(CFG)})
    set_draft(project, "assistant", EDIT)
    ctx = multiprocessing.get_context()
    labels = ("one", "two")
    ready = {label: ctx.Event() for label in labels}
    go = ctx.Event()
    queue: multiprocessing.Queue = ctx.Queue()
    processes = [ctx.Process(target=_child, args=(label, project, ready[label], go, queue)) for label in labels]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(_TIMEOUT) for event in ready.values()), "both children must reach the start barrier"
        go.set()
        # Read results before join: a child blocks on exit until its queue data is consumed.
        results = [queue.get(timeout=_TIMEOUT * 2) for _ in processes]
        for process in processes:
            process.join(_TIMEOUT)
        assert not any(process.is_alive() for process in processes), "children must not hang"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()

    winners = [r for r in results if r[2] is None]
    losers = [r for r in results if r[2] is not None]
    assert len(winners) == 1 and len(losers) == 1, f"expected one success and one failure: {results!r}"
    (winner_label, new_id, _, _), (_, _, loser_exc, loser_text) = winners[0], losers[0]
    assert loser_exc is SchemaError, f"the loser must fail closed with SchemaError: {results!r}"
    assert loser_text == "No changes between drafts and migration head", loser_text

    chain = core.load_revisions(project)
    assert [(r.revision, r.down_revision) for r in chain] == [(A, None), (new_id, A)], chain
    assert chain[-1].message == f"edit {winner_label}"
    files = sorted(p.name for p in (pm_dir(project) / "versions").iterdir())
    assert len(files) == 2 and not [name for name in files if name.endswith(".tmp")], files
    assert chain[-1].path.name in files
