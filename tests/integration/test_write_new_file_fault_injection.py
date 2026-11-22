"""Fault injection (#33): an OSError at any step of `project.write_new_file` never leaves a half-written revision.

Every step is listed in HARD_LINK and FALLBACK. A recording run proves the lists match what the code really does,
so a new step cannot ship without a fault case. Faults go on `project` and on the stdlib calls it makes (`os`,
`pathlib`), never on `core` or `cli`. After each fault `versions/` holds only the old history, the history loads and
the same write then succeeds. Only a crash (a killed process) is out of scope: the no-hard-link fallback is not
crash-atomic, and the one test of a leftover partial file shows that history then fails closed.
"""
import errno
import os
from pathlib import Path

import pytest

from pmtest import CFG, create, pm_dir, write_revision
from promptmigrate import project as project_module
from promptmigrate.core import load_revisions, pretty
from promptmigrate.errors import MigrationError, StorageError

A, B = "a" * 12, "b" * 12
LINK_UNSUPPORTED = OSError(errno.EPERM, "hard links not supported")

# Every call write_new_file makes on a clean run, in order, with the ones that can fail named by label.
HARD_LINK = ["os.open", "write", "os.fsync", "os.close", "os.link", "unlink_temp", "fsync_dir"]
FALLBACK = ["os.open", "write", "os.fsync", "os.close", "os.link",
            "os.open", "write", "os.fsync", "os.close", "unlink_temp", "fsync_dir"]

# (path, label, occurrence): a failure here aborts the write, so nothing may be published.
ABORTS = [("hard_link", label, 1) for label in ("os.open", "write", "os.fsync", "os.close", "os.link")] + \
         [("fallback", label, 2) for label in ("os.open", "write", "os.fsync", "os.close")]
# A failure after the file is visible is swallowed: the revision is published and the write succeeds.
AFTER_PUBLISH = [("hard_link", "unlink_temp", 1), ("hard_link", "fsync_dir", 1),
                 ("fallback", "unlink_temp", 1), ("fallback", "fsync_dir", 1)]


class Faults:
    """Records every instrumented call and raises OSError(EIO) at the chosen (label, occurrence)."""

    def __init__(self, directory, *, fallback=False, fault=None):
        self.directory, self.fallback, self.fault = directory, fallback, fault
        self.calls, self.muted = [], False

    def hit(self, label):
        if self.muted:
            return
        self.calls.append(label)
        if self.fault and self.fault == (label, self.calls.count(label)):
            raise OSError(errno.EIO, f"injected fault at {label} #{self.fault[1]}")

    def install(self, m):
        real_open, real_fsync, real_close, real_link = os.open, os.fsync, os.close, os.link
        real_write, real_fsync_dir, real_unlink = project_module._write_all, project_module._fsync_dir, Path.unlink

        def op(label, real):
            def wrapper(*args, **kwargs):
                self.hit(label)
                return real(*args, **kwargs)
            return wrapper

        def close(fd):
            real_close(fd)  # really close first, so an injected failure cannot leak the descriptor
            self.hit("os.close")

        def link(src, dst, **kwargs):
            self.hit("os.link")
            if self.fallback:
                raise LINK_UNSUPPORTED
            return real_link(src, dst, **kwargs)

        def fsync_dir(directory):
            self.hit("fsync_dir")
            self.muted = True  # the directory fsync opens, syncs and closes a descriptor of its own (POSIX only)
            try:
                real_fsync_dir(directory)
            finally:
                self.muted = False

        def unlink(path, missing_ok=False):
            if path.parent == self.directory:
                self.hit("unlink_temp" if path.name.endswith(".tmp") else "unlink_target")
            return real_unlink(path, missing_ok=missing_ok)

        m.setattr(os, "open", op("os.open", real_open))
        m.setattr(os, "fsync", op("os.fsync", real_fsync))
        m.setattr(os, "close", close)
        m.setattr(os, "link", link)
        m.setattr(project_module, "_write_all", op("write", real_write))
        m.setattr(project_module, "_fsync_dir", fsync_dir)
        m.setattr(Path, "unlink", unlink)


@pytest.fixture
def versions(project):
    """History holds revision A; the target is the file for revision B, whose text is a valid revision."""
    write_revision(project, A, None, {"assistant": create(CFG)})
    directory = pm_dir(project) / "versions"
    item = {"revision": B, "down_revision": A, "message": "edit", "created_at": "2026-10-30T00:00:00+00:00",
            "changes": {"assistant": {"before": CFG, "after": {"messages": []}}}}
    return directory, directory / f"{B}_edit.json", pretty(item)


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def history(project):
    return [r.revision for r in load_revisions(project)]


@pytest.mark.parametrize("path", ["hard_link", "fallback"])
def test_recorded_steps_match_the_fault_table(project, versions, monkeypatch, path):
    directory, target, text = versions
    faults = Faults(directory, fallback=path == "fallback")
    faults.install(monkeypatch)
    project_module.write_new_file(target, text)
    assert faults.calls == (HARD_LINK if path == "hard_link" else FALLBACK)
    assert history(project) == [A, B]


@pytest.mark.parametrize("path,label,n", ABORTS)
def test_a_fault_before_publish_leaves_nothing_and_history_loads(project, versions, monkeypatch, path, label, n):
    directory, target, text = versions
    before = names(directory)
    Faults(directory, fallback=path == "fallback", fault=(label, n)).install(monkeypatch)
    with pytest.raises(StorageError, match=rf"^Cannot write {target.name}: .*injected fault at {label} #{n}"):
        project_module.write_new_file(target, text)
    assert names(directory) == before  # no revision, no partial file, no temp file
    assert history(project) == [A]
    monkeypatch.undo()
    project_module.write_new_file(target, text)  # the same write succeeds afterwards
    assert history(project) == [A, B]


@pytest.mark.parametrize("path,label,n", AFTER_PUBLISH)
def test_a_fault_after_publish_still_publishes_the_revision(project, versions, monkeypatch, path, label, n):
    directory, target, text = versions
    Faults(directory, fallback=path == "fallback", fault=(label, n)).install(monkeypatch)
    project_module.write_new_file(target, text)
    assert target.read_bytes() == text.encode("utf-8")
    assert [name for name in names(directory) if name.endswith(".json")] == [f"{A}.json", target.name]
    assert history(project) == [A, B]  # a leftover `.tmp` is invisible to the `*.json` glob


def test_a_failed_fallback_cleanup_leaves_a_partial_file_that_history_rejects(project, versions, monkeypatch):
    """The documented non-crash-atomic case: the fallback write fails and removing its partial file fails too."""
    directory, target, text = versions
    real_write = project_module._write_all
    writes = []

    def partial_second_write(fd, data):
        writes.append(fd)
        if len(writes) == 2:
            os.write(fd, data[:3])
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(fd, data)

    faults = Faults(directory, fallback=True, fault=("unlink_target", 1))
    faults.install(monkeypatch)
    monkeypatch.setattr(project_module, "_write_all", partial_second_write)
    with pytest.raises(StorageError, match=rf"^Cannot write {target.name}: "):
        project_module.write_new_file(target, text)
    assert target.read_bytes() == text.encode("utf-8")[:3]
    with pytest.raises(MigrationError):  # fail closed: the partial revision is never read as history
        load_revisions(project)
