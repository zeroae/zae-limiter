# View Mode

Display issue details in a formatted summary. Provides a quick overview of issue status, acceptance criteria progress, and key metadata.

## Usage

```bash
# View by issue number
/issue view 135
/issue view #135

# View with comments
/issue view 135 --comments
```

## Process

1. **Identify Issue**: Get issue number from arguments
2. **Fetch Issue Details**:
```bash
gh issue view <number> --repo zeroae/zae-limiter
```
3. **Parse and Format**: Extract key information into structured summary
4. **Display Results**: Present formatted overview to user

## Output Format

Present issue details in this format:

```markdown
## Issue #<number>: <title>

**Status:** <state> | **Milestone:** <milestone> | **Labels:** <labels>

### Summary
<First paragraph or summary section from issue body>

### Acceptance Criteria

| Status | Criterion |
|--------|-----------|
| ✅ | <checked criterion> |
| ⬜ | <unchecked criterion> |

### Additional Sections
<List any other notable sections like Documentation Updates, Implementation notes, etc.>
```

## Acceptance Criteria Parsing

- Extract checkboxes from issue body
- `- [x]` → ✅ (completed)
- `- [ ]` → ⬜ (pending)
- Count and show progress: "X of Y criteria complete"

## Options

| Flag | Description |
|------|-------------|
| `--comments` | Include issue comments in output |

## Example

```
/issue view 135

## Issue #135: ✨ client-side config cache with configurable TTL

**Status:** Open | **Milestone:** v0.5.0 | **Labels:** area/limiter, performance

### Summary
Implement client-side caching for configuration data (system defaults, resource defaults)
to avoid per-request DynamoDB reads.

### Acceptance Criteria (3 of 10 complete)

| Status | Criterion |
|--------|-----------|
| ✅ | Config is cached with configurable TTL |
| ⬜ | Cache hits avoid DynamoDB reads |
| ⬜ | Thread-safe for sync wrapper |
...
```
