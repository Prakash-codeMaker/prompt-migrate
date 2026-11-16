"""Static type checking gate (issue #20, mypy).

TDD characterization for the mypy setup:

* ``pyproject.toml`` must declare a ``[tool.mypy]`` section that targets ``src/``.
* With that config, ``python -m mypy src`` must succeed on the real source tree.
* The config must not blanket-disable checking: a deliberately injected assignment
  type error in a scratch module must still be rejected by mypy.

The first two are RED before the config / type fixes land and GREEN afterwards. The
third guards against "solving" the gate by simply silencing every error.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PYPROJECT = REPO / "pyproject.toml"


def _mypy_importable() -> bool:
    try:
        import mypy  # noqa: F401
        return True
    except ImportError:
        return False


# mypy ships only in the dev extra / `uv run --with mypy`; a runtime-only install
# (stdlib-only) must still be able to run the suite, so skip when it is absent.
pytestmark = pytest.mark.skipif(
    not _mypy_importable(), reason="mypy not installed (dev extra / uv run --with mypy)"
)


def _run_mypy(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mypy", str(target)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )


def test_pyproject_declares_mypy_config_for_src() -> None:
    """The config lives in pyproject.toml and points mypy at src/ (issue #20 contract)."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    cfg = data.get("tool", {}).get("mypy")
    assert cfg is not None, "pyproject.toml must have a [tool.mypy] section"
    files = cfg.get("files")
    assert files is not None, "[tool.mypy] must declare files"
    assert "src" in [str(x) for x in files], "mypy files must include src/"


def test_mypy_passes_on_real_src() -> None:
    """GREEN gate: the configured lenient mypy run on src/ succeeds."""
    proc = _run_mypy(SRC)
    assert proc.returncode == 0, f"mypy src failed:\n{proc.stdout}\n{proc.stderr}"


def test_mypy_rejects_an_injected_type_error(tmp_path: Path) -> None:
    """The gate really checks types: a deliberately bad annotation must be reported."""
    scratch = tmp_path / "scratch.py"
    scratch.write_text("_BROKEN: int = 'not an int'\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(PYPROJECT), str(scratch)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"mypy must reject the injected type error:\n{combined}"
    assert "assignment" in combined, "the error should be an assignment type error"
