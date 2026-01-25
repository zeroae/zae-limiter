# Audit Mode

When arguments are `audit`, `audit --base <branch>`, or `audit NNN`:

Check if code complies with ADR decisions.

**Options:**
- `--base <branch>`: Specify base branch for comparison (diff mode)
- `NNN`: Optional ADR number to audit only that specific ADR (e.g., `audit 013`)

**Use Task with Explore agent in background mode** (`run_in_background: true`). This hides intermediate tool calls from the user's context.

After launching, wait for completion using `TaskOutput`, then present the result.

## Agent Prompt

For each ADR (sequential or parallel), use this prompt:

```
Audit ADR-NNN compliance for this codebase.

**ADR:** docs/adr/NNN-title.md
**Mode:** [diff against <branch> | full scan]
**Files to check:** [list of files or "all src/**/*.py"]

## Compliance Criteria (CRITICAL - follow exactly)

A violation exists ONLY when:
1. The Decision section contains explicit prose requirements (e.g., "must use X", "Repository owns Y")
2. The code contradicts that explicit requirement

A violation does NOT exist when:
- Code has additional methods/parameters not mentioned in the ADR
- Implementation differs from illustrative examples (per ADR-000, code examples are excluded from ADRs)
- The ADR describes patterns without mandating specific implementations

## Instructions

1. Read the ADR Decision section
2. Extract ONLY explicit requirements stated in prose (ignore any code examples)
3. For each requirement, verify the code follows the stated pattern
4. Report ONLY clear contradictions to explicit requirements

Return findings in this format:

| File | Line | ADR | Violation |
|------|------|-----|-----------|
```

## Output Format

After all agents complete, merge results and output:

```
## ADR Audit: ✅ All clear | ❌ N violations

| File | Line | ADR | Violation |
|------|------|-----|-----------|
```

If violations are found, ask if the user wants to:
- Adjust the implementation to comply
- Create a new ADR to supersede the old decision

## CI Output (GitHub Actions)

When running in CI (detected via `CI=true` environment variable), write a result script to `.github/audit-result.sh`. The workflow executes this script to determine pass/fail status.

**Note:** Do not use `.claude/` directory as it is considered sensitive and writes will be blocked.

### Available GitHub Actions Commands

Use these workflow commands in the script for rich CI integration:

| Command | Effect | Fails Job? |
|---------|--------|------------|
| `echo "::notice file=F,line=L::msg"` | Blue ℹ️ annotation | No |
| `echo "::warning file=F,line=L::msg"` | Yellow ⚠️ annotation | No |
| `echo "::error file=F,line=L::msg"` | Red ❌ annotation | No |
| `echo "::group::Title"` | Start collapsible section | No |
| `echo "::endgroup::"` | End collapsible section | No |
| `cat >> $GITHUB_STEP_SUMMARY` | Add markdown to job summary | No |
| `exit 1` | Fail the job | **Yes** |

**Note:** Annotations appear inline on PR diffs when `file` and `line` are specified. `title=` adds a bold header.

### Script Format

**If all checks pass:**

```bash
#!/bin/bash
cat >> $GITHUB_STEP_SUMMARY << 'EOF'
## ✅ ADR Audit: All checks passed

No violations found.
EOF

exit 0
```

**If violations are found:**

```bash
#!/bin/bash
# Inline annotations on the PR diff
echo "::error file=src/foo.py,line=42,title=ADR-013 Violation::Missing audit trail for config changes"
echo "::error file=src/bar.py,line=10,title=ADR-007 Violation::Wrong pattern used"

# Rich markdown summary
cat >> $GITHUB_STEP_SUMMARY << 'EOF'
## ❌ ADR Audit: 2 violations found

| File | Line | ADR | Violation |
|------|------|-----|-----------|
| src/foo.py | 42 | 013 | Missing audit trail for config changes |
| src/bar.py | 10 | 007 | Wrong pattern used |

### Next Steps
- Fix the violations to comply with the ADR
- Or create a new ADR to supersede the decision
EOF

exit 1
```
