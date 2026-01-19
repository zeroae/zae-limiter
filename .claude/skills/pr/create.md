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

### 2. Determine PR Type and Scope

If issue number provided:
```bash
gh issue view <number> --json title,labels,milestone
```

Extract:
- **Type**: From issue title emoji (✨=feat, 🐛=fix, 📋=docs, 🔧=chore)
- **Labels**: Inherit from issue
- **Milestone**: Inherit from issue

If no issue, infer from branch name or ask.

### 3. Generate PR Title

PR titles follow conventional commits format with gitmoji (lowercase after emoji):

| Issue Type | PR Title Format |
|------------|-----------------|
| ✨ Feature | `✨ feat(scope): description` |
| 🐛 Bug | `🐛 fix(scope): description` |
| 📋 Task | `📝 docs: description` or `✅ test: description` |
| 🔧 Chore | `🔧 chore(scope): description` |
| 🔥 Remove | `🔥 chore: description` (for removals) |
| ⚡ Perf | `⚡ perf(scope): description` |
| ♻️ Refactor | `♻️ refactor(scope): description` |

Scope comes from `area/` label (e.g., `area/cli` → `cli`).

### 4. Generate PR Body

```markdown
## Summary
- <bullet points of what changed>

## Test plan
- [ ] <verification step>

Closes #<issue-number>

🤖 Generated with [Claude Code](https://claude.ai/code)
```

### 5. Create the PR

```bash
gh pr create \
  --title "<emoji> <type>(scope): description" \
  --body "<body>" \
  --label "<inherited-labels>" \
  --milestone "<inherited-milestone>"
```

### 6. Push if Needed

```bash
git push -u origin <branch-name>
```

## Output

Return the PR URL.
