# Status Mode

When arguments contain a version (e.g., "0.4.0", "v0.4.0"):

## Instructions

1. **Normalize the version:** If user provides "0.4.0", search for "v0.4.0"

2. **Get milestone details:**
   ```bash
   gh api repos/zeroae/zae-limiter/milestones --jq '.[] | select(.title == "vX.Y.Z") | {title, description, state, open_issues, closed_issues}'
   ```

3. **List all issues in the milestone:**
   ```bash
   gh issue list --milestone "vX.Y.Z" --state all --json number,title,state,labels --jq '.[] | "\(.state)\t#\(.number)\t\(.title)"'
   ```

4. **If there's an epic issue (title starts with "🎯"), get its body for context:**
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
