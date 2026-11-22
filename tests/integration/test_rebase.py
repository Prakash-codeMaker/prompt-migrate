"""PR #15: `rebase` re-parents an unapplied branch after a Git merge (ADR-030).

`load_revisions` stays strict (rejects forks); `rebase` brings its own fork-tolerant
loader and repairs one fork by minting new ids for the moved branch.
"""
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pmtest import CFG, create, pm_dir, run_cli, tree_snapshot, write_revision
from promptmigrate import apply, project as project_mod, revisions as revisions_mod, store
from promptmigrate.commands.rebase import rebase
from promptmigrate.core import load_revisions
from promptmigrate.revisions import _apply_changes, load_revision_graph
from promptmigrate.errors import MigrationConflictError, MigrationError, RevisionGraphError, StorageError

A = "a" * 12
B = "b" * 12
C = "c" * 12


def assistant(content):
    return {"messages": [{"role": "system", "content": content}]}


def fork_project(project):
    """A(root, creates assistant) forked into B (updates assistant) and C (creates other)."""
    write_revision(project, A, None, {"assistant": create(CFG)})
    write_revision(project, B, A, {"assistant": {"before": CFG, "after": assistant("B")}})
    write_revision(project, C, A, {"other": {"before": None, "after": assistant("C")}})


def _dev_applied(project, ids):
    """Manually apply `ids` (a prefix of source history) to the dev state DB.

    Uses the fork-tolerant loader so it works on a forked history.
    """
    graph = load_revision_graph(project)
    revs = {r.revision: r for r in list(graph.trunk) + list(graph.branch_one) + list(graph.branch_two)}
    path = pm_dir(project) / "states" / "dev.sqlite3"
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE applied_revisions ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, revision TEXT UNIQUE NOT NULL, "
        "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE prompts (name TEXT PRIMARY KEY, config TEXT NOT NULL)")
    state = {}
    for rid in ids:
        r = revs[rid]
        _apply_changes(state, r)
        db.execute(
            "INSERT INTO applied_revisions(revision, checksum, applied_at) VALUES(?, ?, ?)",
            (rid, r.checksum, datetime.now(timezone.utc).isoformat()),
        )
        for name, change in r.changes.items():
            value = change["after"]
            if value is None:
                db.execute("DELETE FROM prompts WHERE name=?", (name,))
            else:
                db.execute(
                    "INSERT INTO prompts(name, config) VALUES(?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET config=excluded.config",
                    (name, apply.canonical(value)),
                )
    db.commit()
    db.close()


def moved_revision(project, new_id):
    """Return the parsed moved revision file (globbed by id prefix), or None if absent."""
    matches = list((pm_dir(project) / "versions").glob(f"{new_id}_*.json"))
    if not matches:
        return None
    return json.loads(matches[0].read_text(encoding="utf-8"))


def test_two_branch_merge_rebases_unapplied_branch(project):
    fork_project(project)
    ids = rebase(project, onto=B)
    assert len(ids) == 1
    new_id = ids[0]
    assert new_id not in {A, B, C}
    # Old branch file gone, new v1 file present, linear history.
    assert not (pm_dir(project) / "versions" / f"{C}.json").exists()
    assert moved_revision(project, new_id) is not None
    assert [r.revision for r in load_revisions(project)] == [A, B, new_id]
    item = moved_revision(project, new_id)
    assert item["format_version"] == 1  # moved files are written as v1 (ADR-026)
    assert item["revision"] == new_id
    assert item["down_revision"] == B  # re-parented onto the stay tip
    assert item["message"] == "m"
    assert item["created_at"] == "2026-10-30T00:00:00+00:00"  # carried over
    assert item["changes"] == {
        "other": {"before": None, "after": assistant("C")}  # before recomputed, after carried
    }


def test_rebase_onto_other_branch(project):
    fork_project(project)
    ids = rebase(project, onto=C)
    assert len(ids) == 1
    new_id = ids[0]
    assert [r.revision for r in load_revisions(project)] == [A, C, new_id]
    item = moved_revision(project, new_id)
    assert item["down_revision"] == C
    assert item["changes"] == {
        "assistant": {"before": CFG, "after": assistant("B")}
    }


def test_conflicting_prompt_is_refused_and_writes_nothing(project):
    """Both branches touch `assistant`: rebase refuses and writes nothing (ADR-030)."""
    write_revision(project, A, None, {"assistant": create(CFG)})
    write_revision(project, B, A, {"assistant": {"before": CFG, "after": assistant("B")}})
    write_revision(project, C, A, {"assistant": {"before": CFG, "after": assistant("C")}})
    before = tree_snapshot(project)
    with pytest.raises(MigrationConflictError, match="Both branches touch prompt"):
        rebase(project, onto=B)
    assert tree_snapshot(project) == before  # nothing written
    # Fork still intact: load_revisions still rejects.
    with pytest.raises(RevisionGraphError, match="Branching detected"):
        load_revisions(project)


def test_moving_an_applied_revision_is_refused(project):
    fork_project(project)
    _dev_applied(project, [A, B])  # B (on the move branch when onto=C) is applied
    before = tree_snapshot(project)
    with pytest.raises(MigrationConflictError, match="Cannot move an applied revision"):
        rebase(project, onto=C)  # keep C, move B -> B is applied
    # Nothing written; fork intact.
    assert tree_snapshot(project) == before
    assert (pm_dir(project) / "versions" / f"{B}.json").exists()
    assert (pm_dir(project) / "versions" / f"{C}.json").exists()


def test_onto_is_required_when_neither_branch_is_applied(project):
    fork_project(project)
    with pytest.raises(MigrationConflictError, match="--onto is required"):
        rebase(project)  # no --onto, neither branch applied


def test_onto_naming_the_branch_that_must_move_is_refused(project):
    """B is applied so B must stay; --onto=C names the branch that must move."""
    fork_project(project)
    _dev_applied(project, [A, B])
    with pytest.raises(MigrationConflictError):
        rebase(project, onto=C)


def test_v0_input_is_written_as_v1(project):
    """A v0 moved revision is rewritten as v1 (ADR-030: whatever its previous format)."""
    write_revision(project, A, None, {"assistant": create(CFG)}, format_version=None)
    write_revision(project, B, A, {"assistant": {"before": CFG, "after": assistant("B")}}, format_version=None)
    write_revision(project, C, A, {"other": {"before": None, "after": assistant("C")}}, format_version=None)
    ids = rebase(project, onto=B)
    item = moved_revision(project, ids[0])
    assert item["format_version"] == 1


def test_cli_text_output(project, capsys):
    fork_project(project)
    code, out, err = run_cli(capsys, project, "rebase", "--onto", B)
    assert (code, err) == (0, "")
    assert out.startswith(f"Rebased 1 revision(s) in dev: ")
    assert out.endswith("\n")


def test_cli_json_output(project, capsys):
    fork_project(project)
    code, out, err = run_cli(capsys, project, "--output", "json", "rebase", "--onto", B)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert doc["ok"] is True
    assert doc["command"] == "rebase"
    assert doc["env"] == "dev"
    assert len(doc["data"]["revisions"]) == 1
    assert doc["data"]["revisions"][0] not in {A, B, C}


def test_cli_conflict_exits_4(project, capsys):
    write_revision(project, A, None, {"assistant": create(CFG)})
    write_revision(project, B, A, {"assistant": {"before": CFG, "after": assistant("B")}})
    write_revision(project, C, A, {"assistant": {"before": CFG, "after": assistant("C")}})
    code, _out, err = run_cli(capsys, project, "rebase", "--onto", B)
    assert code == 4
    assert "Both branches touch prompt" in err


def test_cli_missing_onto_exits_4(project, capsys):
    fork_project(project)
    code, _out, err = run_cli(capsys, project, "rebase")
    assert code == 4
    assert "--onto is required" in err


def test_corrupt_ledger_fails_closed(project):
    """An unreadable/corrupt env ledger fails closed with a typed storage error; nothing is written."""
    fork_project(project)
    _dev_applied(project, [A])  # dev ledger has A
    # Corrupt the dev state DB.
    db_path = pm_dir(project) / "states" / "dev.sqlite3"
    db_path.write_bytes(b"not a sqlite database")
    before = tree_snapshot(project)
    with pytest.raises(StorageError, match="dev"):
        rebase(project, onto=B)
    assert tree_snapshot(project) == before


def test_cli_corrupt_ledger_exits_5_with_json_error(project, capsys):
    fork_project(project)
    _dev_applied(project, [A])
    (pm_dir(project) / "states" / "dev.sqlite3").write_bytes(b"not a sqlite database")
    before = tree_snapshot(project)
    code, out, err = run_cli(capsys, project, "--output", "json", "rebase", "--onto", B)
    assert code == 5
    doc = json.loads(out or err)
    assert doc["ok"] is False
    assert (doc["error"]["code"], doc["error"]["exit_code"]) == ("storage", 5)
    assert tree_snapshot(project) == before


def chain_fork_project(project):
    """A forked into B (stay, updates assistant) and the two-revision chain C -> D (move)."""
    D = "d" * 12
    write_revision(project, A, None, {"assistant": create(CFG)})
    write_revision(project, B, A, {"assistant": {"before": CFG, "after": assistant("B")}})
    write_revision(project, C, A, {"other": {"before": None, "after": assistant("C")}})
    write_revision(project, D, C, {"other": {"before": assistant("C"), "after": assistant("D")}})
    return D


def test_rebase_holds_the_project_lock_through_publication(project, monkeypatch):
    """The whole rebase (history read, publish, delete, verify) runs under one project lock."""
    fork_project(project)
    held = []
    depth = {"n": 0}
    real_lock = store._project_lock
    real_write = project_mod.write_new_file

    @contextmanager
    def tracking_lock(project_dir):
        with real_lock(project_dir):
            depth["n"] += 1
            try:
                yield
            finally:
                depth["n"] -= 1

    def checking_write(path, text):
        held.append(depth["n"] > 0)
        return real_write(path, text)

    monkeypatch.setattr(store, "_project_lock", tracking_lock)
    monkeypatch.setattr(project_mod, "write_new_file", checking_write)
    rebase(project, onto=B)
    assert held == [True]


def test_invalid_dependency_on_moved_branch_is_refused_and_writes_nothing(project):
    """C claims `other` already exists (non-null before) but nothing created it: refuse, do not rewrite to None."""
    write_revision(project, A, None, {"assistant": create(CFG)})
    write_revision(project, B, A, {"assistant": {"before": CFG, "after": assistant("B")}})
    write_revision(project, C, A, {"other": {"before": assistant("x"), "after": assistant("C")}})
    before = tree_snapshot(project)
    with pytest.raises(MigrationConflictError, match="invalid revision dependency"):
        rebase(project, onto=B)
    assert tree_snapshot(project) == before


def test_revision_outside_trunk_and_branches_is_refused_before_any_write(project):
    """A disconnected self-parent revision passes the fork checks but must not let rebase touch files."""
    fork_project(project)
    D = "d" * 12
    write_revision(project, D, D, {"third": create(CFG)})
    before = tree_snapshot(project)
    with pytest.raises(RevisionGraphError, match="Disconnected or cyclic"):
        rebase(project, onto=B)
    assert tree_snapshot(project) == before


def test_failed_deletion_restores_the_original_files(project, monkeypatch):
    """D is deleted, then deleting C raises: originals come back and the replacements are removed."""
    chain_fork_project(project)
    before = tree_snapshot(project)
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == f"{C}.json":
            raise OSError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(OSError, match="simulated unlink failure"):
        rebase(project, onto=B)
    monkeypatch.undo()
    assert tree_snapshot(project) == before


def test_failed_final_verification_restores_the_original_files(project, monkeypatch):
    chain_fork_project(project)
    before = tree_snapshot(project)

    def failing_load(project_dir):
        raise RevisionGraphError("simulated verification failure")

    monkeypatch.setattr(revisions_mod, "load_revisions", failing_load)
    with pytest.raises(RevisionGraphError, match="simulated verification failure"):
        rebase(project, onto=B)
    monkeypatch.undo()
    assert tree_snapshot(project) == before
