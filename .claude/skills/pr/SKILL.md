---
name: pr
description: Create pull requests that inherit issue metadata and follow changelog conventions. Use `/pr <issue-number>` for feature PRs or `/pr release <version>` for release prep PRs. Triggers on "create pr", "open pr", "release prep", "cut release".
allowed-tools: Bash(gh:*), Bash(git:*), Read, Grep, Glob, AskUserQuestion, Task
user-invocable: true
---

# Pull Request Skill

Create PRs following project conventions. Supports two modes:

| Mode | Trigger | Purpose |
|------|---------|---------|
| **Feature PR** | `/pr [issue-number]`, "create pr" | Standard PR for features, bugs, tasks |
| **Release Prep PR** | `/pr release <version>`, "release prep", "cut release" | Verify release readiness |

---

## Feature PR Mode

Create PRs with proper metadata inheritance from linked issues.

### Process

1. **Check Git State**
```bash
git log --oneline origin/main..HEAD
git status
```
If no commits, ask user to commit first.

2. **Determine PR Type and Scope**

If issue number provided:
```bash
gh issue view <number> --json title,labels,milestone
```

Extract:
- **Type**: From issue title emoji (✨=feat, 🐛=fix, 📋=docs, 🔧=chore)
- **Labels**: Inherit from issue
- **Milestone**: Inherit from issue

If no issue, infer from branch name or ask.

3. **Generate PR Title**

PR titles follow conventional commits (lowercase, no emoji):

| Issue Type | PR Title Format |
|------------|-----------------|
| ✨ Feature | `feat(scope): description` |
| 🐛 Bug | `fix(scope): description` |
| 📋 Task | `docs: description` or `test: description` |
| 🔧 Chore | `chore(scope): description` |

Scope comes from `area/` label (e.g., `area/cli` → `cli`).

4. **Generate PR Body**
```markdown
## Summary
- <bullet points of what changed>

## Test plan
- [ ] <verification step>

Closes #<issue-number>

🤖 Generated with [Claude Code](https://claude.ai/code)
```

5. **Create the PR**
```bash
gh pr create \
  --title "<type>(scope): description" \
  --body "<body>" \
  --label "<inherited-labels>" \
  --milestone "<inherited-milestone>"
```

6. **Push if Needed**
```bash
git push -u origin <branch-name>
```

---

## Release Prep PR Mode

Verify a milestone is ready to cut a release. Creates a PR that serves as a release checklist.

### Triggers

- `/pr release v0.5.0`
- "prepare release v0.5.0"
- "cut release 0.5.0"
- "release prep for v0.5.0"

### Process

1. **Create Release Branch**
```bash
git checkout main && git pull
git checkout -b release/v<version>
```

2. **Gather Milestone Status**
```bash
# Get milestone info
gh api repos/{owner}/{repo}/milestones --jq '.[] | select(.title == "v<version>")'

# Get all issues in milestone
gh issue list --milestone v<version> --state all --json number,title,state,type

# Get narrative epic (if exists)
gh issue list --milestone v<version> --json number,title,body --jq '.[] | select(.title | startswith("🎯"))'
```

3. **Analyze Changes Since Last Release**
```bash
# Find previous version tag
git describe --tags --abbrev=0

# List commits since previous version
git log v<prev>..HEAD --oneline

# Show changed files
git diff v<prev>..HEAD --stat
```

4. **Check Backwards Compatibility**

Examine public API for breaking changes:
- `src/zae_limiter/__init__.py` - exports
- `src/zae_limiter/models.py` - data models
- `src/zae_limiter/cli.py` - CLI commands

Look for:
- Removed exports
- New required parameters (without defaults)
- Changed method signatures

5. **Generate Release Prep PR**

Title: `chore: release prep v<version>`

Body template (see [release-prep-template.md](release-prep-template.md)):
```markdown
## Release Prep: v<version>

**Theme:** <from milestone description or epic>
**Milestone:** [v<version>](milestone-url)

## Checklist

### Milestone Status
- [ ] All issues closed (<closed>/<total>)
- [ ] Narrative epic updated

### Code Quality
- [ ] CI passing
- [ ] No breaking changes (or documented)
- [ ] Tests cover new features

### Documentation
- [ ] CLAUDE.md updated
- [ ] docs/ updated
- [ ] CHANGELOG will be auto-generated

## Changes in This Release

### Features
- <from commits>

### Bug Fixes
- <from commits>

### Other
- <from commits>

## Open Issues (if any)
- #N: <title>

## Breaking Changes
None / <list if any>

---
🤖 Generated with [Claude Code](https://claude.ai/code)
```

6. **Create the PR**
```bash
gh pr create \
  --title "chore: release prep v<version>" \
  --body "<release-prep-body>" \
  --milestone "v<version>"
```

7. **Report Summary**
```
Release Prep PR created: <url>

Status: Ready / Needs Work
- Open issues: N
- Breaking changes: None / Yes (documented)

Next steps:
1. Review and check off items in the PR
2. Merge when all checks pass
3. Create release tag: git tag v<version> && git push origin v<version>
```

### Verification Steps (Optional)

If `--deploy-aws` flag or user requests verification:

```bash
# Deploy test stack
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter deploy \
  --name test-release-prep \
  --region us-east-1 \
  --permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" \
  --role-name-format "PowerUserPB-{}"

# Run verification based on success criteria from epic

# Cleanup
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter delete \
  --name test-release-prep --yes
```

---

## Output

- **Feature PR**: Return PR URL
- **Release Prep PR**: Return PR URL with release readiness summary

## Reference Files

- [release-prep-template.md](release-prep-template.md) - Full release prep PR template
