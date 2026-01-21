# Issue Creation Rule

**Always use the `/issue` skill for GitHub issue operations.** Never use `gh issue create` directly.

## Required

When creating, updating, or managing GitHub issues, invoke the `/issue` skill:

```
/issue create <description>
/issue update <number>
/issue verify <number>
```

## Why

The `/issue` skill ensures:
- Correct issue type is set (Bug, Feature, Task, Chore, Epic)
- Labels follow project conventions (`area/*`)
- Milestone is assigned based on thematic fit
- Body follows the appropriate template
- Title uses correct gitmoji prefix

## Never Do

```bash
# Don't do this directly
gh issue create --title "..." --body "..."
```

Instead:
```
/issue create <description of what needs to be tracked>
```
