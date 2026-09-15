# Reading CI Signals

A red check is not automatically a failure, and a green one is not automatically a pass. Every
trap below has produced a wrong merge decision in this repo. Verify the signal before acting on
it.

## Never read checks from the legacy combined-status endpoint

`gh api repos/{owner}/{repo}/commits/{sha}/status` returns `{"state": "pending", "count": 0}`
for every commit here, because every check posts as a **check-run**, not a commit status — the
`.statuses[]` array is always empty, and an empty list defaults to `pending`. Codecov included.

Use check-runs:

```bash
gh pr view <n> --json statusCheckRollup
gh api repos/zeroae/zae-limiter/commits/<sha>/check-runs
```

## Codecov posts before it is finished

`codecov/project` is **not accurate until every flag has uploaded**. `ci-tests.yml` uploads four
flags (`unit`, `integration`, `e2e`, `doctest`) from jobs that finish minutes apart, and Codecov
posts a result after each one. Early readings show a large spurious drop that shrinks as the
remaining flags land.

Observed on PR #576: `failure` at 93.49% (−1.43%), then `failure` at −0.75%, then **`success` at
94.24% (+0.00%)** once `integration` uploaded. Judging on the first reading would have blocked a
green PR.

**Wait until the `Tests` workflow run has concluded before reading either codecov check.** When
several entries exist for the same check name, the **latest** is the truth.

Three further codecov failure modes, all seen in one day:

| Symptom | Cause | What to do |
|---|---|---|
| `codecov/project` red, patch green | Base report is stale — the branch is behind `main` | Known noise. Leave it. Not a required check |
| `codecov/patch` red on a diff with full local coverage | A **cancelled** run posted it, having uploaded nothing | Check whether a later run superseded it |
| Spurious drop on a docs-only PR | Path filters mean only `doctest` uploads | Carryforward handles it — see `codecov.yml` (#452, #494) |

`codecov/project` on a stale base is the one standing exception: it is left alone by owner
decision. **`codecov/patch` is a genuine signal** — diagnose it rather than waving it through,
but confirm which run posted it first. Cross-check with `diff-cover --compare-branch=origin/main`
locally, which the pre-push hook already gates at 100%.

## Cancelled runs look like failures

A cancelled check-run renders as a red or `fail 0s` row indistinguishable from a real failure.
Two things cancel runs routinely:

- **A `draft` → `ready_for_review` flip** fires a second full round on the identical commit. The
  first is cancelled by the concurrency group.
- **A new push** supersedes in-flight runs.

A `ready_for_review` flip also makes `mergeStateStatus` read `UNSTABLE` while the duplicate round
runs. That is not a failure.

**Confirm supersession explicitly, never by inferring from duplicate check names:**

```bash
gh api "repos/zeroae/zae-limiter/actions/runs?head_sha=<sha>" \
  --jq '.workflow_runs[] | "\(.id) \(.name) \(.created_at) \(.conclusion)"'
```

A cancelled run and its successor share a workflow name and head SHA and are seconds apart. If
the cancelled row has no successful successor, it is a real failure.

## Job status lags its own steps

A job can report `in_progress` at the run level while every step — including `Complete job` —
has already concluded `success`. Check the steps before calling a job hung:

```bash
gh run view <run-id> --json jobs \
  --jq '.jobs[] | select(.name=="integration (3.12)") | .steps[] | "\(.name) \(.conclusion)"'
```

Typical durations on this repo: `unit` ~7 min, `e2e` ~10 min, `integration` ~15 min, the full
`Tests` round ~15-20 min. Suspect a hang only well past that, and only after the steps disagree
with the job.

## Required checks are narrow

Branch protection requires only `lint`, `build`, and `CodeQL`, with `strict: false` (a branch
need not be up to date to merge). Everything else — unit, integration, e2e, benchmarks, both
codecov checks — is advisory to GitHub, which is why `mergeStateStatus` can read `UNSTABLE` on a
PR that is genuinely fine.

`strict: false` cuts the other way too: **GitHub will let you merge a branch whose green CI
predates changes on `main` that affect it.** Before merging, check whether `main` gained commits
touching the same files since the PR's CI ran; if so, merge `origin/main` into the branch (merge,
never rebase — see `commits.md`) and re-run CI rather than merging on a stale green. A local
trial merge plus a targeted test run, then `git merge --abort`, is cheap insurance.

## Path filters mean some checks never report

`ci-tests.yml` has a `paths:` filter, so a docs-only PR never reports `unit`, `integration` or
`e2e` at all. Those checks are *absent*, not pending — waiting for them to go green is waiting
forever. This is also why they cannot be added to branch protection.

## Do not poll from two places

An implementing agent that polls its own PR to green **and** a merge agent watching the same
checks both burn turns on one wait. Exactly one owner per PR.
