# Scan Mode

Scan open issues for subjective acceptance criteria and flag them for review.

## Usage

```bash
# Scan all open issues
/issue scan

# Scan open issues in a specific milestone
/issue scan --milestone v0.5.0

# Scan and offer to fix (interactive)
/issue scan --fix

# Scan specific issues
/issue scan 133 150 167
```

## Process

1. **Fetch Issues**: Get open issues matching criteria
   ```bash
   # All open issues with acceptance criteria
   gh issue list --state open --json number,title,body,milestone --limit 100

   # Filter by milestone
   gh issue list --state open --milestone "v0.5.0" --json number,title,body --limit 100
   ```

2. **Extract Acceptance Criteria**: For each issue, parse checkboxes from body
   - Look for `## Acceptance Criteria` section
   - Extract all `- [ ]` and `- [x]` items

3. **Detect Subjective Language**: Check each criterion against red flags

   | Pattern | Example |
   |---------|---------|
   | `where beneficial` | "used where beneficial" |
   | `as appropriate` | "handle errors as appropriate" |
   | `as needed` | "update documentation as needed" |
   | `improved` (without metric) | "performance improved" |
   | `better` (without metric) | "better error handling" |
   | `clean` / `cleaner` | "clean code" |
   | `well-*` | "well-documented", "well-tested" |
   | `reasonable` / `acceptable` | "reasonable performance" |
   | `properly` / `correctly` | "properly handles errors" |
   | `good` / `adequate` | "good test coverage" |

4. **Report Findings**: Output summary table

5. **Offer Fixes** (if `--fix`): For each flagged criterion, propose alternatives

## Output Format

```markdown
## Acceptance Criteria Scan Results

### Issues with Subjective Criteria

| Issue | Criterion | Subjective Phrase | Suggested Rewrite |
|-------|-----------|-------------------|-------------------|
| #133 | "Projection expressions used where beneficial" | "where beneficial" | "Projection expressions fetch only attributes necessary and sufficient for execution" |
| #150 | "Code is well-tested" | "well-tested" | "Unit tests cover all public methods" |

### Summary

- **Scanned:** 15 issues
- **Clean:** 12 issues (no subjective criteria)
- **Flagged:** 3 issues (5 subjective criteria)

### Issues Without Acceptance Criteria

| Issue | Title |
|-------|-------|
| #142 | 🐛 Fix timeout in batch operations |
```

## Interactive Fix Mode (`--fix`)

When `--fix` is specified, iterate through flagged criteria and use `AskUserQuestion`:

```
Issue #133: "Projection expressions used where beneficial"

Detected: "where beneficial" (subjective)

How would you like to rewrite this criterion?
- "Projection expressions fetch only attributes necessary and sufficient for execution without changing unit tests"
- "Projection expressions used in all GetItem and Query calls"
- "Skip this criterion"
- Other (specify)
```

After user selects, update the issue:

```bash
gh issue view 133 --json body --jq '.body' > /tmp/issue_body.md
# Apply substitution
gh issue edit 133 --body "$(cat /tmp/issue_body.md)"
```

## Detection Patterns

Regex patterns for subjective language detection:

```python
SUBJECTIVE_PATTERNS = [
    r'\bwhere\s+(beneficial|appropriate|needed|necessary)\b',
    r'\bas\s+(appropriate|needed|necessary)\b',
    r'\b(improved?|better)\b(?!.*[<>=≤≥]\s*\d)',  # without metric
    r'\bwell[-\s]?(documented|tested|designed|structured)\b',
    r'\b(clean|cleaner)\s+(code|implementation)\b',
    r'\b(reasonable|acceptable|adequate)\b',
    r'\b(properly|correctly)\s+\w+',
    r'\bgood\s+(coverage|performance|quality)\b',
]
```

## Rules

- **Read-only by default**: Only report findings unless `--fix` is specified
- **Batch questions**: When fixing, batch related criteria in same issue
- **Preserve formatting**: Only modify criterion text, not surrounding markdown
- **Skip closed issues**: Only scan open issues
- **Respect milestone filter**: When specified, only scan that milestone
