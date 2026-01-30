# Status Mode

Replace `VERSION` below with the milestone version (add "v" prefix if missing, e.g., "0.4.0" → "v0.4.0").

## Step 1: Get milestone details

Run now:
```bash
gh api repos/zeroae/zae-limiter/milestones --jq '.[] | select(.title == "VERSION") | {title, description, state, open_issues, closed_issues}'
```

## Step 2: List all issues

Run now:
```bash
gh issue list --milestone "VERSION" --state all --json number,title,state,labels --jq '.[] | "\(.state)\t#\(.number)\t\(.title)"'
```

## Step 3: Get epic context (if present)

If any issue title starts with "🎯", run:
```bash
gh issue view <epic-number> --json title,body
```

## Output Format

Provide a summary in this format:

```
## Milestone vX.Y.Z: <Description>

**Theme:** <from epic or milestone description>

**Progress:** X closed / Y open issues

### Open Issues

| # | Title | Notes |
|---|-------|-------|
| ... | ... | ... |

### Closed Issues

| # | Title |
|---|-------|
| ... | ... |

### Analysis

<Brief analysis of what's done, what remains, and any blockers>

### Suggested Next Steps

1. <action>
2. <action>
```

## Important

- Keep the summary concise - don't include full issue bodies
- Highlight blockers or dependencies if mentioned in issue titles/bodies
- For epics, check success criteria completion status
