"""Exercise the copyable README quick-start commands without a shell."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
START = "<!-- quickstart-smoke:start -->"
END = "<!-- quickstart-smoke:end -->"


def test_readme_quickstart(tmp_path: Path) -> None:
    """Run the commands against an empty project and report the failing line."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count(START) == readme.count(END) == 1, "Mark one README quick-start block"
    block = readme.split(START, 1)[1].split(END, 1)[0].strip().splitlines()
    assert block and block[0] == "```bash" and block[-1] == "```", "Expected a fenced Bash example"
    commands = [line.strip() for line in block[1:-1] if line.strip() and not line.lstrip().startswith("#")]
    assert commands, "The README quick-start block is empty"

    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    for command in commands:
        entrypoint, *arguments = shlex.split(command)
        assert entrypoint == "promptmigrate", f"Expected a PromptMigrate command, got {command!r}"
        result = subprocess.run(
            [sys.executable, "-m", "promptmigrate", *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{command!r} exited {result.returncode}\n{result.stdout}{result.stderr}"
        )
