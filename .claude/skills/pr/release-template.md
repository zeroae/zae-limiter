# Release Prep PR Template

Full template for release preparation pull requests.

## PR Title

```
🔧 chore: release prep v<version>
```

## PR Body

```markdown
## Release Prep: v<version>

**Theme:** <milestone description or epic theme>
**Milestone:** [v<version>](<milestone-url>)
**Epic:** #<epic-number> (if exists)

---

## Pre-Release Checklist

### Milestone Status
- [ ] All issues closed (<closed>/<total> complete)
- [ ] All PRs merged
- [ ] Narrative epic success criteria met
- [ ] Ad-hoc work tagged to milestone

### Code Quality
- [ ] CI passing on main
- [ ] No regressions in test suite
- [ ] New features have test coverage
- [ ] No security vulnerabilities introduced
- [ ] AWS e2e suite passed on the release commit (`pytest -m aws --run-aws`): <sha>, <date>, <passed>/<total>

### Backwards Compatibility
- [ ] No breaking changes OR breaking changes documented below
- [ ] New parameters have defaults
- [ ] Deprecated features have migration path

### Documentation
- [ ] CLAUDE.md reflects current state
- [ ] docs/ site updated for new features
- [ ] API docstrings complete
- [ ] CLI help text accurate

---

## Changes in This Release

### ✨ Features
- <feat commit summary> (#<issue>)

### 🐛 Bug Fixes
- <fix commit summary> (#<issue>)

### 📝 Documentation
- <docs commit summary>

### 🔧 Chores
- <chore commit summary>

---

## Open Issues

<If all closed: "All issues in milestone are closed.">

<If open issues remain:>
| Issue | Title | Status |
|-------|-------|--------|
| #N | <title> | <blocking/non-blocking> |

---

## Breaking Changes

<If none: "None - all changes are backwards compatible.">

<If breaking changes exist:>
### <Breaking Change Title>

**What changed:** <description>

**Migration path:**
```python
# Before
old_way()

# After
new_way()
```

---

## Verification Results

<If --deploy-aws was used:>
### AWS Deployment Test

- Stack: `test-release-prep`
- Region: `us-east-1`
- Status: ✅ Passed / ❌ Failed

<Results of success criteria verification>

---

## Next Steps

After this PR is merged:

1. Close the narrative epic: `gh issue close <epic-number>`
2. Create the release tag — **annotated**, on the **release-prep commit** (this PR's head,
   whose tree the AWS e2e run covered), not on the merge commit:
   ```bash
   git fetch origin main --tags
   git tag -a v<version> <release-prep-commit> -m "v<version>: <milestone theme>"
   git push origin v<version>
   ```
   `release.yml` refuses a lightweight tag before building anything; if it does, delete the
   tag (`git push --delete origin v<version>`, `git tag -d v<version>`) and re-tag.
3. Verify GitHub Actions creates the release

---

🤖 Generated with [Claude Code](https://claude.ai/code)
```

## Usage Notes

- Fill in `<placeholders>` with actual values
- Remove sections that don't apply (e.g., Breaking Changes if none)
- Check off items as they're verified
- The PR serves as both a checklist and release documentation
