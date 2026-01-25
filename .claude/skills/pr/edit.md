# Edit PR Mode

Update an existing PR's body based on current commits. Useful when PR scope has changed.

## Triggers

- `/pr edit` - Edit PR for current branch
- `/pr edit 195` - Edit specific PR number
- "update pr", "rewrite pr body"

## Process

### 1. Get Current PR

```bash
# If no PR number provided, get PR for current branch
gh pr view --json number,title,body,labels,milestone,url

# Or if PR number provided
gh pr view <number> --json number,title,body,labels,milestone,url
```

### 2. Get Base Branch

```bash
# Get the PR's base branch
gh pr view <number> --json baseRefName --jq '.baseRefName'
```

Use the base branch for commit comparisons (may be `main` or `release/<version>`).

### 3. Analyze Commits

```bash
# Get all commits in PR (use actual base branch)
git log --oneline origin/<base-branch>..HEAD

# Group by type (feat, fix, docs, chore, etc.)
git log --oneline origin/<base-branch>..HEAD --format="%s"
```

### 4. Detect Changed Areas

Use an Explore agent to analyze the changed files and categorize them:

```
Task(Explore): Analyze the files changed in this PR and categorize them by:
- New features added
- Skills added (.claude/skills/)
- Documentation changes (docs/)
- Configuration changes
- Infrastructure changes
```

### 5. Get Linked Issue (if any)

```bash
# Check PR body for "Closes #N" or "Fixes #N"
gh pr view --json body --jq '.body' | grep -oE '(Closes|Fixes) #[0-9]+'
```

### 6. Generate Updated Body

```markdown
## Summary

- <bullet point per logical change group>
- <group related commits together>

## <Section per major change category>

<Details about that category - tables, lists, etc.>

## Test plan

- [ ] <verification steps>

Closes #<issue-number>

🤖 Generated with [Claude Code](https://claude.ai/code)
```

**Body generation guidelines:**
- Group commits by theme, not chronologically
- Use tables for structured data (ADRs, skills, etc.)
- Keep summary bullets high-level (3-6 bullets)
- Add sections for major additions (new skills, new docs, etc.)
- Preserve any "Closes #N" references from original body

### 7. Update the PR

```bash
gh pr edit <number> --body "$(cat <<'EOF'
<generated body>
EOF
)"
```

### 8. Verify Update

```bash
gh pr view <number> --json body --jq '.body' | head -20
```

## Output

```
PR #<number> updated: <url>

Changes detected:
- <N> commits
- <categories affected>
```
