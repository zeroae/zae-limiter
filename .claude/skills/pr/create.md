# Create PR Mode

Create PRs with proper metadata inheritance from linked issues.

## Triggers

- `/pr` - Create PR for current branch
- `/pr 123` - Create PR linked to issue #123
- "create pr", "open pr"

## Process

### 1. Check Git State

```bash
git log --oneline origin/main..HEAD
git status
```

If no commits, ask user to commit first.

### 2. Determine Base Branch

PRs should target the release branch that matches the issue's milestone.

**Step 1: Get milestone from issue (if provided) or infer from branch:**
```bash
# If issue number provided
gh issue view <number> --json milestone --jq '.milestone.title'
# Returns e.g., "v0.5.0" or "0.5.0"
```

**Step 2: Check if release branch exists:**
```bash
# Try both formats: release/0.5.0 and release/v0.5.0
git ls-remote --heads origin "release/${version}" "release/v${version}"
```

**Step 3: Determine base branch:**
- If release branch exists → use `release/<version>` as base
- If release branch does NOT exist → ask user:
  - "Create release branch from main?" → create `release/<version>` from main
  - "Target main directly?" → use `main` as base
  - "Cancel?" → abort PR creation

**Step 4: Update comparison commands:**
```bash
# Use the determined base branch instead of main
git log --oneline origin/<base-branch>..HEAD
```

### 3. Determine PR Type and Scope

If issue number provided:
```bash
gh issue view <number> --json title,labels,milestone
```

Extract:
- **Type**: From issue title emoji (✨=feat, 🐛=fix, 📋=docs, 🔧=chore)
- **Labels**: Inherit from issue
- **Milestone**: Inherit from issue

If no issue, infer from branch name or ask.

### 4. Generate PR Title

PR titles follow the project's commit message conventions.

Scope comes from `area/` label (e.g., `area/cli` → `cli`). If no label, use an Explore agent to infer scope from changed files.

### 5. Generate PR Body

```markdown
## Summary
- <bullet points of what changed>

## Test plan
- [ ] <verification step>

Closes #<issue-number>

🤖 Generated with [Claude Code](https://claude.ai/code)
```

### 6. Create the PR

**All PRs are created in draft mode.**

```bash
gh pr create \
  --draft \
  --base "<base-branch>" \
  --title "<emoji> <type>(scope): description" \
  --body "<body>" \
  --label "<inherited-labels>" \
  --milestone "<inherited-milestone>"
```

### 7. Push if Needed

```bash
git push -u origin <branch-name>
```

## Output

Return the PR URL.
