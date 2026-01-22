# GitHub Skills Rule

**Always invoke skills for GitHub operations.** Never bypass skills by running `gh` commands directly.

## Required Skills

### Issues (`/issue`)

When creating, updating, or managing GitHub issues:

```
/issue create <description>
/issue update <number>
/issue verify <number>
```

The `/issue` skill ensures:
- Correct issue type (Bug, Feature, Task, Chore, Epic)
- Labels follow project conventions (`area/*`)
- Milestone assigned based on thematic fit
- Body follows appropriate template
- Title uses correct gitmoji prefix

### Pull Requests (`/pr`)

When creating or editing pull requests:

```
/pr                     # Create PR for current branch
/pr 123                 # Create PR linked to issue #123
/pr create --base <branch>
/pr edit [pr-number]
```

The `/pr` skill ensures:
- Labels and milestone inherited from linked issue
- PR targets correct base branch (release branch if milestone exists)
- Title follows commit conventions
- Body includes summary, test plan, and issue reference
- PRs created in draft mode

## Never Do

```bash
# Don't bypass skills by running gh commands directly
gh issue create --title "..." --body "..."
gh issue edit 123 --body "..."
gh pr create --title "..." --body "..."
gh pr edit 218 --add-label "..."
```

Instead, invoke the appropriate skill:
```
/issue create <description>
/pr create --base release/0.5.0
```

## Clarification

Skills use `gh` commands internally - that's expected. The rule is: **you must invoke the skill**, not run `gh` commands yourself to bypass it.
