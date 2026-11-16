"""Install each distribution in a fresh venv and exercise its console script."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def clean_env():
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def verify_install(python: Path, cwd: Path) -> None:
    # Import the CLI too: an installed package must not fill missing modules
    # from an editable checkout or a .pth file outside this venv.
    probe = """
import pathlib, sys
import promptmigrate.cli
root = pathlib.Path(sys.prefix).resolve()
for name, module in list(sys.modules.items()):
    if name == 'promptmigrate' or name.startswith('promptmigrate.'):
        # Namespace packages have no __file__; check every portion instead of skipping them.
        file = getattr(module, '__file__', None)
        locations = [file] if file is not None else list(getattr(module, '__path__', []))
        if not locations:
            raise RuntimeError(f'{name} has no file or path to verify')
        for location in locations:
            path = pathlib.Path(location).resolve()
            if not path.is_relative_to(root):
                raise RuntimeError(f'{name} imported from outside the venv: {path}')
print('Verified installed imports:', promptmigrate.cli.__file__)
"""
    subprocess.run([str(python), "-I", "-c", probe], cwd=cwd, env=clean_env(), check=True)


def smoke(artifact: Path) -> None:
    artifact = artifact.resolve(strict=True)
    checkout = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="promptmigrate-package-") as temporary:
        root = Path(temporary).resolve()
        if root.is_relative_to(checkout):
            raise RuntimeError("Package smoke tests must run outside the checkout")
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        cli = binary / ("promptmigrate.exe" if os.name == "nt" else "promptmigrate")
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "--no-deps", str(artifact)],
            cwd=root, env=clean_env(), check=True,
        )
        verify_install(python, root)
        project = root / "project"
        project.mkdir()

        def run_text(*args):
            result = subprocess.run(
                [str(cli), *args], cwd=project,
                env=clean_env(), text=True, capture_output=True,
            )
            if result.returncode:
                print(result.stdout, end="")
                print(result.stderr, file=sys.stderr, end="")
                result.check_returncode()
            print(f"{artifact.name}: {' '.join(args)} passed", flush=True)
            return result.stdout

        def run(*args):
            result = subprocess.run(
                [str(cli), "--output", "json", *args], cwd=project,
                env=clean_env(), text=True, capture_output=True,
            )
            if result.returncode:
                print(result.stdout, end="")
                print(result.stderr, file=sys.stderr, end="")
                result.check_returncode()
            payload = json.loads(result.stdout)
            if payload["schema"] != "promptmigrate.output/1" or not payload["ok"]:
                raise RuntimeError(f"Unexpected CLI result: {result.stdout}")
            print(f"{artifact.name}: {' '.join(args)} passed", flush=True)
            return payload["data"]

        run("init")
        config = {"messages": [{"role": "system", "content": "Package smoke test."}]}
        (project / ".promptmigrate" / "drafts" / "assistant.json").write_text(
            json.dumps(config), encoding="utf-8",
        )
        run("revision", "-m", "package smoke")
        if not run_text("validate").startswith("OK: "):
            raise RuntimeError("Installed validate did not print an OK: line")
        if run("validate")["revisions"] != 1:
            raise RuntimeError("Installed validate did not report the round-trip revision")
        run("upgrade")
        run("check", "--require-head")
        history = run("history")
        if len(history["revisions"]) != 1:
            raise RuntimeError("Expected one installed-package revision")
        if run("get", "assistant")["config"] != config:
            raise RuntimeError("Installed CLI did not round-trip the draft")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="+")
    for artifact in parser.parse_args().artifacts:
        smoke(artifact)


if __name__ == "__main__":
    main()
