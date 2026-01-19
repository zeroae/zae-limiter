# Release PR Mode

Create release preparation PRs for version milestones.

## Triggers

- `/pr release <version>` - Create release PR for specified version
- Branch name `release/X.X.X` or `release/vX.X.X` - Auto-detected as release PR
- "release prep", "prepare release"

## Branch Detection

When `/pr` is invoked without arguments, check if current branch is a release branch:

```bash
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" =~ ^release/v?([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    version="${BASH_REMATCH[1]}"
    # Proceed with release mode using extracted version
fi
```

## Process

### 1. Extract Version

From branch name or argument:
- `release/0.5.0` → version `0.5.0`
- `release/v1.2.3` → version `1.2.3`
- `/pr release 0.5.0` → version `0.5.0`

### 2. Get Milestone Info

```bash
gh api repos/:owner/:repo/milestones --jq '.[] | select(.title == "v<version>" or .title == "<version>")'
```

Extract:
- Description (theme)
- Open/closed issue counts
- URL

### 3. Get Commits Since Last Release

```bash
# Find previous tag
git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo "v0.0.0"

# Get commits since then
git log --oneline <previous-tag>..HEAD
```

### 4. Check for Open Issues

```bash
gh issue list --milestone "v<version>" --state open --json number,title
```

### 5. Check for Breaking Changes

Scan commits for:
- `BREAKING CHANGE:` in commit bodies
- `!` after type (e.g., `feat(api)!:`)
- `💥` emoji

```bash
git log --format="%B" <previous-tag>..HEAD | grep -E "(BREAKING CHANGE:|^[a-z]+\([^)]*\)!:)"
```

### 6. Generate PR

Use template from [release-prep-template.md](release-prep-template.md).

```bash
gh pr create \
  --title "chore: release prep v<version>" \
  --body "<generated-body>" \
  --milestone "v<version>"
```

### 7. Invoke Release-Prep Skill

After creating the PR, suggest running the `release-prep` skill for full verification:

```
Run `/release-prep <version>` for complete milestone verification.
```

## Output

```
Release PR created: <url>

Version: v<version>
Theme: <milestone description>
Commits: <N> since <previous-tag>
Open issues: <N>
Breaking changes: <yes/no>
```
