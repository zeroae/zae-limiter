# Verify Mode

Strictly verify acceptance criteria by running actual tests/checks against the codebase. Unlike Progress mode (which matches work evidence), Verify mode confirms criteria are actually MET.

## Usage

```bash
# Default: verify and auto-check passing criteria
/issue verify 150

# Dry-run: verify only, don't modify the issue
/issue verify 150 --dry-run
```

## Process

1. **Identify Issue**: Get issue number from arguments or context
2. **Fetch Issue Details**:
```bash
gh issue view <number>
```
3. **Extract Acceptance Criteria**: Parse checkboxes from issue body
4. **Create Todo List**: Add each criterion as a pending todo item
5. **Verify Each Criterion Strictly**:
   - Mark current criterion as `in_progress`
   - Run actual verification (code inspection, runtime checks, grep, etc.)
   - Mark as `completed` with PASS/FAIL/PARTIAL status
   - Move to next criterion
6. **Report Results**: Show summary table with status and evidence
7. **Auto-Check** (unless `--dry-run`): Update issue to check passing criteria

## Verification Approach

For each checkbox, determine appropriate verification:

| Criterion Type | Verification Method |
|----------------|---------------------|
| "X defined in file Y" | `grep` for definition in specified file |
| "X method exists" | `python -c "import ...; hasattr(...)"` |
| "X parameter accepted" | `python -c "import inspect; ..."` |
| "Tests pass" | `pytest` or check test files exist |
| "Documentation updated" | `grep` for content in docs |
| "Warning emitted" | Runtime test with `warnings.catch_warnings` |

## Strict Verification Rules

- **PASS**: Criterion is fully met with evidence
- **FAIL**: Criterion is NOT met (missing, wrong name, etc.)
- **PARTIAL**: Criterion is partially met (document specifics)

## Example Verification Session

```
Verifying issue #150: Extract Repository Protocol

Acceptance Criteria:
1. [in_progress] RepositoryProtocol defined in repository_protocol.py

   Checking: grep "^class RepositoryProtocol" src/zae_limiter/repository_protocol.py
   Result: Found at line 26
   Status: ✅ PASS

2. [pending] Repository.ensure_infrastructure() method exists

   Checking: python -c "from zae_limiter import Repository; print(hasattr(Repository, 'ensure_infrastructure'))"
   Result: False (create_stack exists instead)
   Status: ❌ FAIL - Method does not exist

...

## Summary
| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | RepositoryProtocol in repository_protocol.py | ✅ PASS | Line 26 |
| 2 | Repository.ensure_infrastructure() | ❌ FAIL | create_stack exists instead |
...
```

## Auto-Check Passing Criteria

**Behavior:**
- **Default**: After verification, update issue body to check `- [x]` for all PASS criteria
- **`--dry-run`**: Report results only, don't modify the issue

**IMPORTANT: Only modify checkboxes**
- The ONLY change allowed is converting `- [ ]` to `- [x]` for passing criteria
- Do NOT modify any other text in the issue body
- Do NOT add comments, summaries, or verification results to the issue
- Do NOT change formatting, whitespace, or any non-checkbox content
- Parse the exact checkbox line, replace only `[ ]` with `[x]`, preserve everything else

## Update Issue (non-dry-run)

After verification completes, update the issue body by ONLY changing checkboxes:

1. Fetch current body: `gh issue view <number> --json body --jq '.body'`
2. For each PASS criterion, find the exact line `- [ ] <text>` and replace with `- [x] <text>`
3. Leave ALL other content unchanged (no additions, no reformatting)
4. Update issue:

```bash
gh issue edit <number> --body "$(cat <<'EOF'
<body with ONLY checkbox changes - nothing else modified>
EOF
)"
```

## Output

Return a markdown table:

```markdown
## Issue #<number> Verification Results

| # | Criterion | Status | Details |
|---|-----------|--------|---------|
| 1 | <criterion text> | ✅ PASS | <evidence> |
| 2 | <criterion text> | ❌ FAIL | <what's wrong> |
| 3 | <criterion text> | ⚠️ PARTIAL | <what's missing> |

**Summary: X PASS, Y FAIL, Z PARTIAL**

[If not --dry-run]
✅ Checked off 8 passing criteria in issue #150
```
