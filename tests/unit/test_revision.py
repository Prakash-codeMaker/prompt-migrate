"""PR 22: `revision` emits a non-blocking `DeletionWarning` when a generated change deletes a config."""
import warnings

from pmtest import CFG, create, pm_dir, write_revision
from promptmigrate import core
from promptmigrate.errors import DeletionWarning


def test_revision_emits_deletion_warning(project):
    """A generated revision that deletes a config warns (non-blocking) and points at --allow-delete."""
    write_revision(project, "a" * 12, None, {"assistant": create(CFG)})
    # Remove the draft so the replayed head has `assistant` but the drafts do not -> a deletion.
    (pm_dir(project) / "drafts" / "assistant.json").unlink()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.revision(project, "delete assistant")
    msgs = [str(w.message) for w in caught if issubclass(w.category, DeletionWarning)]
    assert msgs, f"expected a DeletionWarning, got: {[str(w.message) for w in caught]}"
    assert "assistant" in msgs[0], msgs[0]


def test_revision_no_deletion_warning_when_creating(project):
    """Creating a config emits no DeletionWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.revision(project, "create assistant")
    assert not any(issubclass(w.category, DeletionWarning) for w in caught), \
        [str(w.message) for w in caught]
