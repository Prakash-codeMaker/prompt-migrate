"""`revision`: write a new revision file from the drafts' difference to the source head."""

from __future__ import annotations

import argparse
import re
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from .. import draftdiff, output, project, protocol, revisions, secretscan
from ..errors import (
    DeletionWarning,
    LegacyRevisionWarning,
    SchemaError,
    SecretDetectedError,
    SecretInHistoryWarning,
    StorageError,
)
from ..store import lock

_ID_ATTEMPTS = 5


def _new_id(taken: set[str]) -> str:
    """A fresh 12-hex revision id that is not already in history."""
    for _ in range(_ID_ATTEMPTS):
        rid = uuid.uuid4().hex[:12]
        if rid not in taken:
            return rid
    raise StorageError(f"Could not generate a unique revision id after {_ID_ATTEMPTS} attempts")


def revision(project_dir: Path, message: str) -> revisions.Revision:
    if not message.strip():
        raise SchemaError("Revision message cannot be empty")
    # History is read inside the lock, so two concurrent `revision` calls serialize and the second one diffs against
    # the first one's new head (I18).
    with lock._project_lock(project_dir):
        return _write_revision(project_dir, message)


def _write_revision(project_dir: Path, message: str) -> revisions.Revision:
    all_revisions = revisions.load_revisions(project_dir)
    changes = draftdiff.draft_diff(project_dir)
    if not changes:
        raise SchemaError("No changes between drafts and migration head")
    rid = _new_id({r.revision for r in all_revisions})
    removed = [name for name, change in changes.items() if change["after"] is None]
    if removed:
        listed = ", ".join(sorted(removed))
        warnings.warn(
            f"Revision {rid} would delete {listed}; they show as `delete` in `plan`/`diff`.",
            DeletionWarning,
            stacklevel=3,
        )
    parent = all_revisions[-1].revision if all_revisions else None
    for name, change in changes.items():
        if change["after"] is not None:
            found = protocol.config_violations(name, change["after"])
            if found:
                raise SchemaError(f"New revision would be invalid: {', '.join(found)}")
    allow = project.load_allowlist(project_dir)
    findings = [finding for name, change in changes.items() if change["after"] is not None
                for finding in secretscan.scan(name, change["after"], allow)]
    findings += secretscan.scan("(message)", {"message": message.strip()}, allow)
    if findings:
        raise SecretDetectedError(findings)
    item = {
        "format_version": 1,
        "revision": rid,
        "down_revision": parent,
        "message": message.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    }
    text = project.pretty(item)
    # write_new_file writes these UTF-8 bytes with LF line endings on every OS (ADR-026), so this is its exact size.
    size = len(text.encode("utf-8"))
    if size > protocol.limits.MAX_REVISION_BYTES:
        raise SchemaError(f"New revision would be {size} bytes; the limit is {protocol.limits.MAX_REVISION_BYTES}")
    for name, change in changes.items():
        if change["before"] is not None:
            carried = protocol.config_violations(name, change["before"])
            if carried:
                warnings.warn(f"revision {rid}: {name} before carries {', '.join(carried)} from history "
                              "(copied for compatibility)", LegacyRevisionWarning, stacklevel=3)
    for name, change in changes.items():
        if change["before"] is not None:
            for finding in secretscan.scan(name, change["before"], allow):
                warnings.warn(f"{finding.name} {finding.path} {finding.rule} copied from history into before; "
                              "the credential must be rotated", SecretInHistoryWarning, stacklevel=3)
    root = project.require_init(project_dir)
    path = root / "versions" / f"{rid}_{re.sub(r'[^a-z0-9]+', '_', message.lower()).strip('_')[:50] or 'revision'}.json"
    try:
        project.write_new_file(path, text)
    except FileExistsError:
        raise StorageError(f"Revision file already exists: {path.name}") from None
    return revisions.load_revisions(project_dir)[-1]


def execute(args: argparse.Namespace) -> output.Result:
    """Run `revision` and return its text and JSON result."""
    r = revision(Path(args.project), args.message)
    data = {"revision": r.revision, "down_revision": r.down_revision, "message": r.message, "path": str(r.path)}
    return output.Result("revision", data, f"Created {r.revision} (parent: {r.down_revision or 'base'}): {r.path}\n")


def run(args: argparse.Namespace) -> None:
    """Run `revision` and print its text output."""
    output.emit_result(execute(args), "text", [])
