#!/usr/bin/env bash
#
# Create a git worktree that can actually run the test suite and the git hooks.
#
#   scripts/new-worktree.sh <branch> [base]
#
# A bare `git worktree add` produces a checkout with no `.venv`. `uv run` will
# not rescue it: `pytest-asyncio` (and everything else the tests import) lives in
# the `dev` **extra**, not a dependency group, so a plain `uv run pytest` installs
# neither. The failure surfaces at the worst moment — the pre-push hook dies with
# `ModuleNotFoundError: No module named 'pytest_asyncio'` and the push is refused,
# which reads like a broken branch rather than a missing environment.
#
# Each worktree gets its **own** venv rather than sharing or symlinking the main
# checkout's. That is the whole point: the editable install must resolve
# `zae_limiter` to *this* worktree's `src/`. Share one venv and every measurement
# taken in a worktree silently describes `main` instead of the branch under test —
# a real session lost a day to exactly that, reporting a docs page green when its
# own branch was red.
#
# uv hardlinks from a shared cache, so the per-worktree cost is seconds and very
# little disk.

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: scripts/new-worktree.sh <branch> [base]" >&2
    echo "  e.g. scripts/new-worktree.sh fix/123-thing            # base: origin/main" >&2
    echo "       scripts/new-worktree.sh docs/foo origin/release  # explicit base" >&2
    exit 2
fi

branch="$1"
base="${2:-origin/main}"

repo_root="$(git rev-parse --show-toplevel)"
# Strip any worktree suffix: run from inside a worktree, --show-toplevel points at
# the worktree, not the primary checkout.
main_root="$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir)"
main_root="$(dirname "$main_root")"

slug="${branch//\//-}"
dest="$main_root/.claude/worktrees/$slug"

if [[ -e "$dest" ]]; then
    echo "error: $dest already exists" >&2
    exit 1
fi

echo "==> fetching $base"
git -C "$main_root" fetch --quiet origin

echo "==> creating worktree $dest on $branch (from $base)"
git -C "$main_root" worktree add -q "$dest" -b "$branch" "$base"

echo "==> syncing venv (uv sync --all-extras)"
( cd "$dest" && uv sync --all-extras )

cat <<EOF

Worktree ready:

    cd $dest

Its .venv resolves zae_limiter to that worktree's src/, so tests and the
pre-push hook measure the branch rather than main.
EOF
