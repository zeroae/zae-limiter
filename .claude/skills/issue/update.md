# Update Mode

Update an existing issue's body or metadata.

## Process

1. **Identify Issue**: Infer issue number from context or ask
2. **Fetch Current State**:
```bash
gh issue view <number> --json title,body,labels,milestone
```
3. **Determine Changes**: From conversation, identify what to add/modify
4. **Preview Changes**: Show diff of what will change
5. **Apply Update**

## Issue Number Inference

Detect issue references from context:
- Explicit: "#123", "issue 123", "issue #123"
- From branch name: `git branch --show-current` → extract issue number
- From recent commits: `git log -1 --format=%s` → look for "Closes #N"
- From conversation: recently discussed issue numbers

## Update Commands

**Update body:**
```bash
gh issue edit <number> --body "$(cat <<'EOF'
<updated body>
EOF
)"
```

**Update metadata:**
```bash
gh issue edit <number> --add-label "<label>" --milestone "<milestone>"
```

**Update title:**
```bash
gh issue edit <number> --title "<new title>"
```

## Output

Return the updated issue URL with summary of changes.
