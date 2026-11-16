"""PR 11 (cycle 2): `validate` checks source only; warnings fail only under `--strict` (ADR-028, I5, I19).

`validate` loads history, drafts, the environment manifest (when present) and the allowlist, and audits secrets.
It never resolves an environment and never reads, creates or modifies anything under `states/`.
"""
import json

import pytest

from pmtest import (
    CFG, LEGACY_ID, LEGACY_NAMES, LEGACY_WARNINGS, create, install_legacy, pm_dir, run_cli, set_draft,
    tree_snapshot, write_revision,
)
from promptmigrate import store
from promptmigrate.core import revision, upgrade

B = "b" * 12


def _dup_key_revision(project):
    path = pm_dir(project) / "versions" / f"{LEGACY_ID}.json"
    path.write_text('{"revision": "a", "revision": "b"}', encoding="utf-8")


def _nan_revision(project):
    path = write_revision(project, LEGACY_ID, None, {"assistant": create()})
    path.write_text(path.read_text(encoding="utf-8").replace('"A"', "NaN"), encoding="utf-8")


def _bad_format_version(project):
    write_revision(project, LEGACY_ID, None, {"assistant": create()}, format_version=2)


def _missing_parent(project):
    write_revision(project, LEGACY_ID, "f" * 12, {"assistant": create()})


def _two_roots(project):
    write_revision(project, LEGACY_ID, None, {"assistant": create()})
    write_revision(project, B, None, {"other": create()})


def _bad_draft(project):
    (pm_dir(project) / "drafts" / "assistant.json").write_text('{"a": 1, "a": 2}', encoding="utf-8")


def _bad_manifest(project):
    (pm_dir(project) / "environments.json").write_text('{"environments": []}', encoding="utf-8")


def _bad_allowlist(project):
    (pm_dir(project) / "secret-allowlist.json").write_text("[]", encoding="utf-8")


TIER_A = {
    "duplicate_key_revision": _dup_key_revision,
    "non_finite_revision": _nan_revision,
    "unsupported_format_version": _bad_format_version,
    "missing_parent": _missing_parent,
    "two_roots": _two_roots,
    "invalid_draft": _bad_draft,
    "invalid_manifest": _bad_manifest,
    "invalid_allowlist": _bad_allowlist,
}


def test_clean_project_exits_0_with_summary(project, capsys):
    revision(project, "first")
    code, out, err = run_cli(capsys, project, "validate")
    rid = json.loads(run_cli(capsys, project, "--output", "json", "history")[1])["data"]["revisions"][0]["revision"]
    assert (code, err) == (0, "")
    assert out == f"OK: 1 revisions (v0: 0, v1: 1), head {rid}\n"


def test_empty_history_reports_base(project, capsys):
    assert run_cli(capsys, project, "validate")[:2] == (0, "OK: 0 revisions (v0: 0, v1: 0), head base\n")


def test_json_data_counts_format_versions(project, capsys):
    write_revision(project, LEGACY_ID, None, {"assistant": create()})
    set_draft(project, "assistant", CFG)
    code, out, _ = run_cli(capsys, project, "--output", "json", "validate")
    doc = json.loads(out)
    assert code == 0
    assert doc["data"] == {"revisions": 1, "head": LEGACY_ID, "format_versions": {"0": 1, "1": 0},
                           "drafts_clean": None}


@pytest.mark.parametrize("name", sorted(TIER_A))
def test_tier_a_error_exits_3(project, capsys, name):
    TIER_A[name](project)
    code, out, err = run_cli(capsys, project, "validate")
    assert (code, out) == (3, "")
    assert err.startswith("Error: ")


@pytest.mark.parametrize("name", [n for n in LEGACY_NAMES if LEGACY_WARNINGS[n]])
def test_legacy_tier_b_warns_and_exits_0(project, capsys, monkeypatch, name):
    install_legacy(project, monkeypatch, name)
    code, out, err = run_cli(capsys, project, "validate")
    assert code == 0 and out.startswith("OK: ")
    assert "Warning: " in err


@pytest.mark.parametrize("name", [n for n in LEGACY_NAMES if LEGACY_WARNINGS[n]])
def test_legacy_tier_b_fails_under_strict(project, capsys, monkeypatch, name):
    install_legacy(project, monkeypatch, name)
    code, out, err = run_cli(capsys, project, "validate", "--strict")
    assert (code, out) == (3, "")
    assert "Warning: " in err and "validate --strict: " in err


def test_secret_in_history_exits_0_and_3_under_strict(project, capsys, monkeypatch):
    install_legacy(project, monkeypatch, "secret_in_history")
    code, _, err = run_cli(capsys, project, "validate")
    assert code == 0 and "Warning: revision " in err
    code, _, err = run_cli(capsys, project, "validate", "--strict")
    assert code == 3 and "Error: validate --strict: 1 warning(s)" in err


def test_strict_counts_distinct_warnings(project, capsys, monkeypatch):
    install_legacy(project, monkeypatch, "reserved_types")
    code, out, _ = run_cli(capsys, project, "--output", "json", "validate", "--strict")
    doc = json.loads(out)
    assert code == 3
    assert doc["error"]["message"] == "validate --strict: 2 warning(s)"
    assert len(doc["warnings"]) == 2


def test_unrevisioned_draft_edit_fails_only_with_drafts_clean(project, capsys):
    revision(project, "first")
    set_draft(project, "assistant", {"messages": [{"role": "system", "content": "B"}]})
    assert run_cli(capsys, project, "validate")[0] == 0
    code, out, err = run_cli(capsys, project, "validate", "--drafts-clean")
    assert (code, out) == (3, "")
    assert "Error: Drafts differ from head: assistant" in err


def test_drafts_clean_reports_true_in_json(project, capsys):
    revision(project, "first")
    code, out, _ = run_cli(capsys, project, "--output", "json", "validate", "--drafts-clean")
    assert code == 0 and json.loads(out)["data"]["drafts_clean"] is True


def test_uninitialized_project_exits_1(tmp_path, capsys):
    assert run_cli(capsys, tmp_path, "validate")[0] == 1


@pytest.mark.parametrize("with_manifest", [False, True])
def test_validate_never_touches_states(project, capsys, monkeypatch, with_manifest):
    revision(project, "first")
    upgrade(project, "dev")
    if not with_manifest:
        (pm_dir(project) / "environments.json").unlink()
    before = tree_snapshot(project)

    def boom(*_a, **_k):
        raise AssertionError("validate touched states/")

    monkeypatch.setattr(store.sqlite, "_states_dir", boom)
    code, out, err = run_cli(capsys, project, "validate", "--drafts-clean", "--strict")
    assert (code, err) == (0, "")
    assert tree_snapshot(project) == before


def test_validate_does_not_create_files(tmp_path, capsys):
    from promptmigrate.core import init
    init(tmp_path)
    before = tree_snapshot(tmp_path)
    assert run_cli(capsys, tmp_path, "validate")[0] == 0
    assert tree_snapshot(tmp_path) == before
