"""Project-wide lock serializing manifest changes, revision writes, and initial environment database setup."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
import threading

from .. import project
from ..errors import StorageError

# Project roots whose lock this thread already holds. POSIX flock on a second descriptor blocks against the
# first, so a nested lock (restamp around connect_write) would deadlock; the Windows mutex is re-entrant already.
_held = threading.local()


@contextmanager
def _project_lock(project_dir: Path) -> Iterator[None]:
    """Serialize project metadata changes, revision writes, and initial environment database setup."""
    root = project.require_init(project_dir)
    # sys.platform, not os.name: mypy narrows it, so each branch is only checked for its own platform
    if sys.platform == "win32":
        import ctypes
        digest = hashlib.sha256(os.fsencode(root)).hexdigest()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.CreateMutexW(None, False, f"Global\\PromptMigrate-{digest}")
        if not handle:
            raise StorageError(
                f"Could not create environment manifest mutex: Windows error {ctypes.get_last_error()}"
            ) from None
        result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        if result not in (0, 0x80):
            kernel.CloseHandle(handle)
            raise StorageError(
                f"Could not acquire environment manifest mutex: Windows error {ctypes.get_last_error()}"
            ) from None
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        held = _held.__dict__.setdefault("roots", set())
        if root in held:
            yield
            return
        descriptor = os.open(root, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held.add(root)
            try:
                yield
            finally:
                held.discard(root)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
