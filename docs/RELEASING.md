# Releasing PromptMigrate

Maintainer guide for publishing the `promptmigrate-cli` package to PyPI. The import package and the command are both `promptmigrate`; only the PyPI distribution name differs, because `promptmigrate` was already taken.

This is about shipping the **Python package**. It is not the prompt release activation described in [EVALUATION_AND_RELEASES.md](EVALUATION_AND_RELEASES.md), which is not shipped. A published package says nothing about production readiness: `--env prod` is still a local SQLite file, and the "Not shipped" list in [`AGENTS.md`](../AGENTS.md) still applies to the README and release notes.

## How a release works

Pushing a tag that matches `v*` starts [`.github/workflows/release.yml`](../.github/workflows/release.yml):

1. `build` checks that the tag equals `v` + the `pyproject.toml` version, builds the wheel and sdist, runs `scripts/package_smoke.py` on both, and uploads them as the `dist` artifact.
2. `publish` downloads that artifact and publishes it from the `pypi` GitHub environment with trusted publishing (OIDC). No API token is stored anywhere. The publish action is pinned to a commit SHA because that job holds `id-token: write`.

Nothing reaches PyPI unless `build` passed. A given version can be published only once (see [If something goes wrong](#if-something-goes-wrong)).

## One-time setup

Already done for `promptmigrate-cli`; kept here so it can be rebuilt or audited.

- **PyPI publisher.** On PyPI, a trusted publisher for the project with: owner `himanshu231204`, repository `prompt-migrate`, workflow `release.yml`, environment `pypi`. Before the first upload it is a *pending* publisher; the first successful publish turns it into a normal one. A typo in any field fails only at publish time.
- **GitHub environment.** Settings → Environments → `pypi`, with deployment branches and tags set to *Selected branches and tags* and a **tag** rule `v*`. "Protected branches only" rejects tag refs, and "No restriction" lets any branch run the publish job. Optionally add yourself as a required reviewer for a manual approval step.

## Before tagging

Changes reach `main` only through a pull request, so the version bump and changelog edit are a PR (`chore/release-X.Y.Z`), merged before tagging.

1. **`main` is green.** The latest `ci-pass` on `main` passed, and so did the latest weekly full Windows/macOS matrix run. A red weekly run is fixed first.
2. **Bump the version in both places.** `pyproject.toml` (`version`) and `src/promptmigrate/__init__.py` (`__version__`, the fallback `--version` reports when the package is not installed). The workflow checks only `pyproject.toml`, so confirm they match:

   ```bash
   grep -n '^version' pyproject.toml
   grep -n '__version__ =' src/promptmigrate/__init__.py
   ```

   Then refresh `uv.lock`, which records the project's own version, and check it:

   ```bash
   uv lock
   uv lock --check
   git diff uv.lock    # only the promptmigrate-cli version line should change
   ```

   If other lines changed (for example dependency markers), your `uv` differs from the one that wrote the lock; restore the file from `main` and edit only the version line by hand.

3. **Cut the changelog.** `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and add a fresh empty `## [Unreleased]` above it. Check that every CLI or file contract change is marked **Contract:** (pre-1.0 contracts may change), and keep each entry to one line, `**Name** — description (#N)`.
4. **Update the roadmap.** Tick finished items in `docs/ROADMAP.md` and refresh the status lines and "First next step".
5. **Re-check claims.** README, changelog and release notes must not mention anything on the "Not shipped" list in `AGENTS.md`, and must not claim production readiness.
6. **Build and smoke-test locally** (a throwaway virtualenv is fine):

   ```bash
   python -m pip install --upgrade build
   python -m build
   python scripts/package_smoke.py dist/*.whl dist/*.tar.gz
   ```

   Delete `dist/` afterwards; it is not committed.
7. **Merge the release PR** with a merge commit, after its checks pass.

## Cut the release

Tag the merge commit on `main`, not a feature branch:

```bash
git fetch origin
git tag -a vX.Y.Z -m "vX.Y.Z" origin/main
git push origin vX.Y.Z
```

Watch the run in the Actions tab (`release`). If the `pypi` environment has a required reviewer, approve the `publish` job when asked.

## Verify

1. The new version appears at <https://pypi.org/project/promptmigrate-cli/>.
2. In a fresh virtualenv:

   ```bash
   python -m pip install promptmigrate-cli==X.Y.Z
   promptmigrate --version    # promptmigrate X.Y.Z
   ```

3. Confirm the tag exists (`git tag -l vX.Y.Z`), then create a GitHub Release from it with the changelog section as its notes (`gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <notes>`), and check it with `gh release view vX.Y.Z`.
4. For the first release only: close #55 and tick the PyPI item in `docs/ROADMAP.md`. The documented upgrade policy (#56) is still open.

## If something goes wrong

- **`build` failed (tag/version mismatch, smoke test, build error).** Nothing was published. Fix on `main` through a PR, then move the tag:

  ```bash
  git tag -d vX.Y.Z
  git push origin :refs/tags/vX.Y.Z
  ```

  and tag again as above. Only do this when `publish` never ran.
- **`publish` rejected by the environment** ("not allowed to deploy to pypi"). The environment's tag rule is missing or wrong; fix it under Settings → Environments → `pypi`, then re-run the failed job.
- **`publish` rejected by PyPI** (trusted publisher mismatch). Compare the publisher's owner, repository, workflow and environment against [One-time setup](#one-time-setup), then re-run the job.
- **A bad version was published.** PyPI never accepts the same filename twice, even after a delete. Yank the release on PyPI, fix forward, and ship `X.Y.(Z+1)`. Never retag a version that reached PyPI.

## Versioning

The project is pre-1.0 (`0.y.z`). CLI and file contracts may change between minor versions; such changes are marked **Contract** in `CHANGELOG.md`. Changing the revision file format or checksum semantics needs an explicit format version, not just a release. The policy for upgrading existing projects across releases is tracked in #56 and is not written yet.
