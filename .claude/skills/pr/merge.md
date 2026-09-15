# Merge PR Mode

Merge a pull request that is genuinely green, then clean up after it.

## Merge strategy — this repo allows merge commits only

```bash
gh pr merge <number> --merge --delete-branch
```

**Never `--squash`, never `--rebase`.** The repository has `allow_squash_merge: false` and
`allow_rebase_merge: false`; either flag is rejected outright, and a reflexive `--squash` attempt
wastes a call and prints an error that reads like the merge failed. Confirm if unsure:

```bash
gh api repos/zeroae/zae-limiter \
  --jq '"squash=\(.allow_squash_merge) merge=\(.allow_merge_commit) rebase=\(.allow_rebase_merge)"'
```

Preserving individual commits is not incidental — `git-cliff` builds the changelog from commit
messages alone, so a squashed `fix(scope): … Fixes #NNN` would vanish from Bug Fixes. See
`.claude/rules/commits.md`.

## Before merging

1. **Poll every check-run to a conclusion.** Use a bounded loop with a sleep, or `gh run watch`.
   Never write a loop that `pgrep`s for its own command line — it matches itself and can never
   exit.

2. **Read the signals correctly.** See `.claude/rules/ci-signals.md`. In short: read check-runs,
   never the legacy combined-status endpoint; a cancelled run is not a failure; `codecov/project`
   is not accurate until every flag has uploaded; `codecov/project` against a stale base is known
   noise, while `codecov/patch` is a genuine signal.

3. **Check for a stale green.** Branch protection sets `strict: false`, so GitHub will let you
   merge a branch whose CI ran before `main` gained commits affecting the same files. Compare
   what landed since the branch point against what the PR touches:

   ```bash
   git log --oneline <merge-base>..origin/main
   git diff --name-only <merge-base>...<head>
   ```

   On real overlap, merge `origin/main` into the branch (**merge, never rebase** — see
   `.claude/rules/commits.md` on not rewriting history) and re-run CI to conclusion. With no
   overlap, a local trial merge plus a targeted test run, then `git merge --abort`, is cheap
   insurance and is worth taking anyway.

4. **Check what will actually close — the body AND the commits.**

   ```bash
   gh pr view <number> --json closingIssuesReferences          # body only
   git log <base>..<head> --format='%B' \
     | grep -inE '(clos|fix|resolv)[a-z]*[[:space:]]+#[0-9]+'  # commit messages
   ```

   GitHub's parser matches closing keywords as substrings, ignores negation and quotation, and
   reads **commit messages landing on the default branch** as well as the PR body. The `gh` query
   sees only the body, so it cannot catch a directive in a commit message — which is how an epic
   was closed by the very commit documenting this trap. Run both.

   See `.claude/rules/pull-request-workflow.md`.

## After merging

Verify, do not assume:

- The intended issue is CLOSED.
- Any epic the PR merely referenced is **still OPEN**.
- Every original commit survives as an ancestor of `main`.
- The remote branch is gone (`delete_branch_on_merge` usually handles it).

Then clean up:

```bash
git fetch --prune origin
git worktree remove .claude/worktrees/<name>     # if one exists
git merge --ff-only origin/main                  # so -d can see the merge
git branch -d <branch>
```

**Never `git branch -D`.** If `-d` refuses, it is usually because local `main` has not been
fast-forwarded yet — do that and retry. If it still refuses, stop and report; do not force.

## Report

Final head SHA, merge commit SHA, which issues closed and which stayed open, the CI picture
including how any cancelled or superseded runs were resolved, whether `main` was merged in and
why, and cleanup status.
