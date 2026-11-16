"""`validate`: check every source file (history, drafts, manifest, allowlist) without touching environment state."""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path
from typing import Any

from .. import draftdiff, output, project, protocol, revisions, store
from ..errors import MigrationConflictError, SchemaError, StrictValidationError
from .check import audit_secrets


def validate(project_dir: Path, strict: bool = False, drafts_clean: bool = False) -> dict[str, Any]:
    """Validate source only (ADR-028); never resolves an environment or reads anything under `states/` (I19)."""
    root = project.require_init(project_dir)
    states_dir = root / "states"
    # Guard: a revision symlink that resolves under states/ would make validate read applied state.
    for path in sorted((root / "versions").glob("*.json")):
        try:
            if path.resolve().is_relative_to(states_dir.resolve()):
                raise SchemaError(f"Invalid revision {path.name}: resolves under states/")
        except (RuntimeError, OSError):
            # A symlink loop or unreadable path cannot resolve under states/; the loader will report it.
            pass

    caught: list[warnings.WarningMessage] = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                revisions_ = revisions.load_revisions(project_dir)
            except MigrationConflictError as exc:
                # A source-history replay conflict is a tier-A source error for `validate`: exit 3, not 4.
                raise SchemaError(f"Invalid source history: {exc}") from exc
            project.read_drafts(project_dir)
            # Without a manifest, read_environments infers declarations from states/, which validate must never read.
            # Read the manifest only when present; never fall back to states/ inference (I19).
            manifest = root / "environments.json"
            if manifest.exists() or manifest.is_symlink():
                _read_manifest_only(manifest, root)
            project.load_allowlist(project_dir)
            audit_secrets(project_dir)
            changed = sorted(draftdiff.draft_diff(project_dir)) if drafts_clean else []
    finally:
        for w in caught:  # re-emit, so the CLI still prints or lists them even on partial failure
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


def _read_manifest_only(manifest: Path, root: Path) -> None:
    """Read `environments.json` without falling back to states/ inference (I19).

    Mirrors the structural validation of store.read_environments but never calls _infer_environments,
    so a concurrently removed manifest cannot make validate enumerate states/.
    """
    value, _raw, _rules = protocol.load_json(manifest, kind="environments", root=root)
    if not isinstance(value, dict) or set(value) != {"environments"} or not isinstance(value["environments"], dict):
        raise SchemaError("Invalid environments file: expected exactly {'environments': object}")
    environments = value["environments"]
    for name, metadata in environments.items():
        protocol.rules.validate_environment_name(name)
        if not isinstance(metadata, dict):
            raise SchemaError(f"Invalid environments file: metadata for {name!r} must be an object")
        if protocol.config_depth(metadata) > protocol.limits.MAX_CONFIG_DEPTH:
            raise SchemaError(f"Invalid environments file: metadata for {name!r} exceeds depth limit")
    collisions = protocol.case_collisions(environments)
    if collisions:
        raise SchemaError(f"Invalid environments file: case-collision {collisions[0]}")


def execute(args: argparse.Namespace) -> output.Result:
    """Run `validate` and return its text and JSON result."""
    data = validate(Path(args.project), args.strict, args.drafts_clean)
    v = data["format_versions"]
    text = f"OK: {data['revisions']} revisions (v0: {v['0']}, v1: {v['1']}), head {data['head'] or 'base'}\n"
    return output.Result("validate", data, text)


def run(args: argparse.Namespace) -> None:
    """Run `validate` and print its text output."""
    output.emit_result(execute(args), "text", [])
