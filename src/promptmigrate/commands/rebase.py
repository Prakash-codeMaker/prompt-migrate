"""`rebase`: re-parent an unapplied branch after a Git merge, minting new ids (ADR-030, #15)."""

from __future__ import annotations

import argparse
import re
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path

from .. import output, project, revisions, store
from ..errors import MigrationConflictError, SchemaError, StorageError
from ..store import sqlite as sqlite_store


def _new_id(taken: set[str]) -> str:
    """A fresh 12-hex revision id not already in history (the generator `revision` uses)."""
    for _ in range(5):
        rid = uuid.uuid4().hex[:12]
        if rid not in taken:
            return rid
    raise StorageError("Could not generate a unique revision id after 5 attempts")


def _slug(message: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:50] or "revision"


def _v1_item(rid: str, parent: str | None, message: str, created_at: str,
             changes: dict) -> dict:
    return {
        "format_version": 1,
        "revision": rid,
        "down_revision": parent,
        "message": message,
        "created_at": created_at,
        "changes": changes,
    }


def _write_revision_file(root: Path, rid: str, parent: str | None, message: str,
                         created_at: str, changes: dict) -> Path:
    """Write one moved revision as a v1 file atomically; never overwrite an existing file."""
    item = _v1_item(rid, parent, message, created_at, changes)
    text = project.pretty(item)
    path = root / "versions" / f"{rid}_{_slug(message)}.json"
    try:
        project.write_new_file(path, text)
    except FileExistsError:
        raise StorageError(f"Revision file already exists: {path.name}") from None
    return path


def _touched_prompt_names(revisions_: Iterable[revisions.Revision]) -> set[str]:
    """The set of prompt names a branch touches (in any `before` or `after`)."""
    names: set[str] = set()
    for r in revisions_:
        names.update(r.changes.keys())
    return names


def _applied_ids(project_dir: Path) -> set[str]:
    """Union of ledger revision ids across every declared environment (read-only, no state DB created).

    Runs under the project lock so a concurrent writer cannot change a ledger between the environments we inspect.
    """
    environments, _existed = store.read_environments(project_dir)
    applied: set[str] = set()
    with store._project_lock(project_dir):
        for env in environments:
            with store.connect_readonly(project_dir, env) as db:
                if db is None:
                    continue  # base state: nothing applied
                try:
                    applied.update(
                        rid for rid, _checksum in db.execute(
                            "SELECT revision, checksum FROM applied_revisions ORDER BY seq"
                        ).fetchall()
                    )
                except sqlite3.Error as exc:
                    raise sqlite_store.StorageError(
                        f"Cannot read environment state {env!r}: {exc}"
                    ) from exc
    return applied


def rebase(project_dir: Path, onto: str | None = None) -> list[str]:
    """Repair one fork: keep the branch ending at `onto`, move the other onto it.

    Returns the new ids of moved revisions, oldest first. Writes nothing on conflict; a failure while
    publishing, deleting or verifying restores the original files.
    """
    root = project.require_init(project_dir)
    # One lock from the history read to the final verify, so a concurrent rebase sees the already-linear history.
    with store._project_lock(project_dir):
        return _rebase_locked(project_dir, root, onto)


def _rebase_locked(project_dir: Path, root: Path, onto: str | None) -> list[str]:
    """The body of `rebase`; the caller holds the project lock."""
    graph = revisions.load_revision_graph(project_dir)

    branch_one = graph.branch_one
    branch_two = graph.branch_two
    tip_one = branch_one[-1].revision
    tip_two = branch_two[-1].revision

    applied = _applied_ids(project_dir)
    one_has_applied = bool({r.revision for r in branch_one} & applied)
    two_has_applied = bool({r.revision for r in branch_two} & applied)

    if one_has_applied and two_has_applied:
        raise MigrationConflictError(
            "Both branches have applied revisions; neither can be moved"
        )

    # Resolve which branch stays and which moves.
    if onto is not None:
        if not re.fullmatch(r"[a-f0-9]{12}", onto):
            raise SchemaError(f"Unknown --onto revision: {onto}")
        if onto not in (tip_one, tip_two):
            raise SchemaError(f"--onto must name a branch tip: {onto}")
        if onto == tip_one:
            stay_branch, move_branch = branch_one, branch_two
        else:
            stay_branch, move_branch = branch_two, branch_one
    elif one_has_applied:
        stay_branch, move_branch = branch_one, branch_two
    elif two_has_applied:
        stay_branch, move_branch = branch_two, branch_one
    else:
        raise MigrationConflictError(
            "--onto is required when neither branch has applied revisions"
        )

    move_ids = {r.revision for r in move_branch}
    if move_ids & applied:
        raise MigrationConflictError("Cannot move an applied revision")

    # Conflict check: moved branch vs stay branch touched-prompt sets.
    stay_touched = _touched_prompt_names(stay_branch)
    move_touched = _touched_prompt_names(move_branch)
    conflict = sorted(move_touched & stay_touched)
    if conflict:
        quoted = ", ".join(f"{name!r}" for name in conflict)
        raise MigrationConflictError(
            f"Both branches touch prompt(s): {quoted}"
        )

    # Fail closed on a moved revision whose own `before` values never held, before replacing any snapshot.
    for branch in (stay_branch, move_branch):
        revisions.replay_revisions(list(graph.trunk) + list(branch))

    # Compute new ids and recompute `before` snapshots from the replayed new parent.
    # The moved branch is re-parented onto the stay branch tip, so the state to replay is
    # trunk + stay branch (root through the stay tip).
    taken = {r.revision for r in graph.revisions.values()}
    state = revisions.replay_revisions(list(graph.trunk) + list(stay_branch))
    parent = stay_branch[-1].revision
    planned: list[tuple[revisions.Revision, str, dict, str | None]] = []  # (old, new id, new changes, new down_revision)

    for old in move_branch:
        new_rid = _new_id(taken)
        taken.add(new_rid)
        new_changes: dict = {}
        for name, change in old.changes.items():
            new_changes[name] = {
                "before": state.get(name),
                "after": change["after"],
            }
        planned.append((old, new_rid, new_changes, parent))
        # Apply this moved revision to the running state (its after becomes the next before).
        for name, change in old.changes.items():
            if change["after"] is None:
                state.pop(name, None)
            else:
                state[name] = change["after"]
        parent = new_rid

    # All new files are written first; old files removed tip-first; then verified. Any failure puts the
    # original bytes back and removes the replacements.
    old_files = [(old.path, old.path.read_bytes()) for old in reversed(move_branch)]
    new_paths: list[Path] = []
    try:
        for old, new_rid, new_changes, new_parent in planned:
            new_paths.append(_write_revision_file(
                root, new_rid, new_parent, old.message, old.created_at, new_changes,
            ))
        for path, _data in old_files:
            path.unlink()
        revisions.replay_revisions(revisions.load_revisions(project_dir))
    except BaseException:
        for path in new_paths:
            path.unlink(missing_ok=True)
        for path, data in old_files:
            if not path.exists():
                path.write_bytes(data)
        raise

    return [new_rid for _old, new_rid, _changes, _parent in planned]


def execute(args: argparse.Namespace) -> output.Result:
    """Run `rebase` and return its text and JSON result."""
    project_dir = Path(args.project)
    label = store.display_name(project_dir, args.env)
    ids = rebase(project_dir, args.onto)
    data = {"onto": args.onto, "revisions": ids}
    text = f"Rebased {len(ids)} revision(s) in {label}: {', '.join(ids) or '(none)'}\n"
    return output.Result("rebase", data, text, label)


def run(args: argparse.Namespace) -> None:
    """Run `rebase` and print its text output."""
    output.emit_result(execute(args), "text", [])
