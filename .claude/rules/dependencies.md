# Runtime Dependencies and conda-forge

zae-limiter ships on conda-forge as well as PyPI. **When you add a runtime dependency to
`[project] dependencies` in `pyproject.toml`, or raise its floor, check conda-forge at that
moment** — not at release time.

At release time the tag, the GitHub release and the PyPI upload have already happened, and the
only lever left is someone else's feedstock. That is what happened in #222: `cronsim>=2.7` and
`tzdata` were added without a check, and the v0.14.0 conda-forge build was blocked after the
release had shipped everywhere else (conda-forge/zae-limiter-feedstock#25, unblocked by
conda-forge/cronsim-feedstock#3). Checking when the dependency was added would have cost
minutes.

## The check

`scripts/check_conda_forge.py` does all four steps below, and the `conda-forge-check` workflow
runs it on every pull request that changes `pyproject.toml`. Run it yourself before pushing:

```bash
uv run python scripts/check_conda_forge.py
```

### 1. Query the API, never the web page

```bash
curl -s https://api.anaconda.org/package/conda-forge/<name> \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['latest_version'], d['versions'])"
```

`https://anaconda.org/conda-forge/<name>` returns **HTTP 200 for packages that do not exist**, so
a status-code check against the web page proves nothing. The API returns 404. That exact mistake
is how #222 slipped through.

### 2. Resolve the conda-forge name first

PyPI and conda-forge names diverge, and the wrong name can be worse than a missing one:

| PyPI name | conda-forge name | What a naive lookup does |
|-----------|------------------|--------------------------|
| `aws-lambda-builders` | `aws_lambda_builders` | 404 — a false "missing" |
| `tzdata` | `python-tzdata` | **finds the wrong package** — conda-forge `tzdata` is the raw IANA database (versions like `2026c`), so the check passes against something zae-limiter never imports |

The script resolves names through prefix.dev's
[parselmouth](https://github.com/prefix-dev/parselmouth) conda-forge → PyPI map, inverted. A
name the map does not know is looked up by spelling and reported as **verify by hand**.

### 3. Check the constraint, not just existence

Compare the version range in `pyproject.toml` against the `versions` list. A package present at
an older version is the same blocker as one that is absent: conda-forge had `cronsim` 2.6 when
we required `>=2.7`, and 2.6 is not API-compatible.

### 4. Check that the version publishes an sdist

conda-forge recipes conventionally build from the PyPI sdist (`/packages/source/...tar.gz`). A
wheel-only release breaks the recipe URL **and** silently stops the autotick bot, which is why
`cronsim-feedstock` sat un-bumped from February 2025. A known wheel-only dependency whose
feedstock builds from another source goes in `NO_SDIST_OK` in the script, with the reason and a
link.

## When the check fails: open the fix now

| Situation | Fix |
|-----------|-----|
| Package not on conda-forge at all | A `conda-forge/staged-recipes` PR |
| Feedstock exists but is too old | A version-bump PR on `conda-forge/<name>-feedstock` |
| Upstream publishes no sdist | Bump PR that sources the GitHub tag archive instead (see cronsim-feedstock#3) |

**Check for `conda-forge/<name>-feedstock` before opening a staged-recipes PR.** A staged-recipes
PR for a package that already has a feedstock is rejected. Also watch for a raised
`requires-python` floor upstream — cronsim 2.7 moved to `>=3.10` and its feedstock needed a local
`python_min` pin.
