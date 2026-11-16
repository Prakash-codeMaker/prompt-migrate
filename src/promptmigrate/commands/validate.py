"""`validate`: check every source file (history, drafts, manifest, allowlist) without touching environment state."""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path
from typing import Any

from .. import draftdiff, output, project, revisions, store
from ..errors import SchemaError, StrictValidationError
from .check import audit_secrets


def validate(project_dir: Path, strict: bool = False, drafts_clean: bool = False) -> dict[str, Any]:
    """Validate source only (ADR-028); never resolves an environment or reads anything under `states/` (I19)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        revisions_ = revisions.load_revisions(project_dir)
        project.read_drafts(project_dir)
        # Without a manifest, read_environments infers declarations from states/, which validate must never read.
        manifest = project.require_init(project_dir) / "environments.json"
        if manifest.exists() or manifest.is_symlink():
            store.read_environments(project_dir)
        project.load_allowlist(project_dir)
        audit_secrets(project_dir)
        changed = sorted(draftdiff.draft_diff(project_dir)) if drafts_clean else []
    for w in caught:  # re-emit, so the CLI still prints or lists them
        warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)
    if changed:
        raise SchemaError(f"Drafts differ from head: {', '.join(changed)}")
    count = len(output.collect_warnings(caught))
    if strict and count:
        raise StrictValidationError(f"validate --strict: {count} warning(s)")
    versions = {"0": 0, "1": 0}
    for r in revisions_:
        versions[str(r.format_version)] += 1
    return {
        "revisions": len(revisions_),
        "head": revisions_[-1].revision if revisions_ else None,
        "format_versions": versions,
        "drafts_clean": True if drafts_clean else None,
    }


def execute(args: argparse.Namespace) -> output.Result:
    """Run `validate` and return its text and JSON result."""
    data = validate(Path(args.project), args.strict, args.drafts_clean)
    v = data["format_versions"]
    text = f"OK: {data['revisions']} revisions (v0: {v['0']}, v1: {v['1']}), head {data['head'] or 'base'}\n"
    return output.Result("validate", data, text)


def run(args: argparse.Namespace) -> None:
    """Run `validate` and print its text output."""
    output.emit_result(execute(args), "text", [])
