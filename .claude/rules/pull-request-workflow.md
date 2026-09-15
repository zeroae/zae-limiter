# Pull Request Workflow

All changes must go through pull requests. Direct commits to `main` are not allowed.

## Workflow

1. Create a feature branch from main:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/your-feature-name
   ```

2. Make changes following project conventions:
   - Follow commit message conventions (see commits.md)
   - Add tests for new functionality
   - Update documentation as needed

3. Push and create a pull request:
   ```bash
   git push origin feat/your-feature-name
   ```

4. Wait for CI checks to pass:
   - **Lint**: Code style and formatting (ruff)
   - **Type Check**: Static type checking (mypy)
   - **Tests**: Unit tests with coverage (pytest on Python 3.11 & 3.12)

5. Address review feedback if needed

6. Once approved and CI passes, the PR will be merged to main

**Important:** Never force-push to main or bypass CI checks.

## Closing keywords are matched as substrings, in commit messages too

GitHub's linked-issue parser has no notion of negation or quotation. A body reading

```
Does not close #NNN.
```

registers `#NNN` in `closingIssuesReferences` and **closes that issue on merge** — the "Does
not" is invisible to it.

**This applies to commit messages landing on the default branch exactly as it does to PR
bodies.** That half is what bites, because the usual check only inspects the PR:

```bash
gh pr view <n> --json closingIssuesReferences   # PR body only — does NOT see commit messages
```

Both happened in one day on the scheduled-limits epic. First a PR body carried the negated
phrasing. Then the PR that *documented that trap* closed the same epic again — this time from a
commit message quoting the example, which the PR-body check could not see.

So:

- To reference an issue without closing it, write **`Refs #NNN`** or **`Part of #NNN`**.
- Never place `close`/`closes`/`closed`/`fix`/`fixes`/`fixed`/`resolve`/`resolves`/`resolved`
  next to an issue number you do not intend to close — **including inside a denial, a quotation,
  or an example**.
- **Write documentation about closing keywords with a placeholder** (`#NNN`), never a live issue
  number. An example containing a real number is a live directive that propagates into every
  commit message and PR body that quotes it.
- Check the commits, not just the PR body, before merging anything that references an epic:

```bash
git log <base>..<head> --format='%B' | grep -inE '(clos|fix|resolv)[a-z]*[[:space:]]+#[0-9]+'
```

Verify the epic is still open **after** the merge as well. A reopen is cheap; a silently closed
epic is not.

## Reading CI before you merge

See `ci-signals.md`. A red check is frequently a cancelled run, a partial codecov upload, or a
stale base rather than a failure — and `strict: false` means GitHub will let you merge a green
that predates changes on `main`.
