"""ADR-031 (issue #22): `revision` warns (non-blocking) when the generated diff deletes a named config.

`upgrade`/`downgrade` apply deletions freely — there is no apply-time gate per ADR-031. The destructive moment is
surfaced at authoring time (`revision` emits `DeletionWarning`) and preview time (`plan`/`diff` list `delete`).
"""
import pytest

from pmtest import CFG, pm_warnings, set_draft
from promptmigrate.core import downgrade, get_config, revision, upgrade
from promptmigrate.errors import DeletionWarning


def _delete_assistant_draft(project) -> None:
    (project / ".promptmigrate" / "drafts" / "assistant.json").unlink()


def test_revision_warns_on_deletion(project):
    set_draft(project, "assistant", CFG)
    revision(project, "create assistant")
    upgrade(project, "dev")
    assert get_config(project, "dev", "assistant") == CFG

    _delete_assistant_draft(project)
    _, warnings_ = pm_warnings(revision, project, "delete assistant")
    assert any("would delete" in w and "assistant" in w for w in warnings_), warnings_


def test_revision_no_warning_without_deletion(project):
    set_draft(project, "assistant", CFG)
    _, warnings_ = pm_warnings(revision, project, "create assistant")
    assert not any(issubclass(w.category, DeletionWarning) for w in warnings_), warnings_


def test_upgrade_applies_deletion_freely(project):
    set_draft(project, "assistant", CFG)
    revision(project, "create assistant")
    upgrade(project, "dev")
    assert get_config(project, "dev", "assistant") == CFG

    _delete_assistant_draft(project)
    rev = revision(project, "delete assistant")

    # no flag required; the deletion is applied
    assert upgrade(project, "dev") == [rev.revision]
    with pytest.raises(Exception) as exc:
        get_config(project, "dev", "assistant")
    assert "No prompt named" in str(exc.value)


def test_downgrade_applies_deletion_freely(project):
    set_draft(project, "assistant", CFG)
    rev = revision(project, "create assistant")
    upgrade(project, "dev")
    assert get_config(project, "dev", "assistant") == CFG

    # reverting a creation deletes it — allowed without any flag
    assert downgrade(project, "dev", "-1") == [rev.revision]
    with pytest.raises(Exception) as exc:
        get_config(project, "dev", "assistant")
    assert "No prompt named" in str(exc.value)


def test_plan_lists_deletion_kind(project):
    set_draft(project, "assistant", CFG)
    revision(project, "create assistant")
    upgrade(project, "dev")
    assert get_config(project, "dev", "assistant") == CFG

    from promptmigrate.core import plan

    _delete_assistant_draft(project)
    revision(project, "delete assistant")  # generates the deletion revision; not yet applied
    result = plan(project, "dev", "head")
    assert result.direction == "upgrade", result
    kinds = {kind for step in result.steps for _name, kind in step.changes}
    assert "delete" in kinds, result
    names = {name for step in result.steps for name, _kind in step.changes}
    assert "assistant" in names, result
