# Ralph Loop Mode

Iteratively work on an issue until all acceptance criteria pass verification.

## Usage

```bash
/issue ralph-loop <number> [--max-iterations <n>]
```

## Arguments

- `<number>` - GitHub issue number (required)
- `--max-iterations <n>` - Maximum iterations before stopping (default: 10)

## Implementation

When triggered, this mode invokes the Ralph Loop plugin with `/issue verify` as the exit condition:

```bash
/ralph-loop "Work on issue #<number>. For each iteration:
1. Run /issue verify <number> --dry-run to see which criteria are failing
2. Pick one FAILING criterion and implement/fix to satisfy it
3. Run /issue verify <number> to check progress and update the issue
4. If ALL criteria pass (0 FAIL), output <promise>ISSUE RESOLVED</promise>
5. Otherwise, continue working on remaining failing criteria" \
  --completion-promise "ISSUE RESOLVED" \
  --max-iterations <n>
```

## Process

This mode combines issue-driven development with the Ralph Wiggum technique:

1. **Verify Current State**: Run `/issue verify <number> --dry-run` to see failing criteria
2. **Work on Failing Criterion**: Pick one and implement/fix to satisfy it
3. **Verify and Update**: Run `/issue verify <number>` to check and update issue
4. **Loop or Complete**: If all pass, output promise; otherwise continue

## Completion Promise

The loop completes when Claude outputs:

```
<promise>ISSUE RESOLVED</promise>
```

This is automatically set as `--completion-promise` when invoking the Ralph Loop plugin.
Claude should ONLY output this promise when `/issue verify` reports **0 FAIL** criteria.

## Example Session

```
/issue ralph-loop 133 --max-iterations 15
```

**Iteration 1:**
```
Running /issue verify 133 --dry-run...

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Benchmark baseline established | ❌ FAIL |
| 2 | BatchGetItem used for cascade | ❌ FAIL |
| ... | ... | ... |

Working on: "Benchmark baseline established before changes"
- Creating tests/benchmark/test_read_patterns.py...
- Adding baseline test cases...

Running /issue verify 133...
Criterion 1 now passes. Checking off in issue.
Continuing to next iteration...
```

**Iteration 2:**
```
Running /issue verify 133 --dry-run...

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Benchmark baseline established | ✅ PASS |
| 2 | BatchGetItem used for cascade | ❌ FAIL |
| ... | ... | ... |

Working on: "BatchGetItem used for cascade scenarios"
- Adding batch_get_buckets() to repository.py...
- Updating limiter.py to use batch reads...

Running /issue verify 133...
Criterion 2 now passes. Checking off in issue.
Continuing to next iteration...
```

**Final Iteration:**
```
Running /issue verify 133 --dry-run...

| # | Criterion | Status |
|---|-----------|--------|
| 1-6 | All criteria | ✅ PASS |

Summary: 6 PASS, 0 FAIL, 0 PARTIAL

All acceptance criteria verified!
<promise>ISSUE RESOLVED</promise>
```

## Behavior Rules

1. **Use /issue verify**: Always use `/issue verify` to check criteria status, not manual inspection
2. **One criterion per iteration**: Focus on a single failing criterion each loop
3. **Verify before checking off**: Run `/issue verify` (not --dry-run) to update the issue
4. **No false promises**: Only output `<promise>ISSUE RESOLVED</promise>` when verify reports 0 FAIL
5. **Document blockers**: If stuck on a criterion, document what's blocking and continue

## State Tracking

Progress is tracked via:
- Issue checkboxes (persistent across sessions)
- Git commits (code changes)
- Todo list (current session)

## Integration with Verify Mode

This mode delegates all verification to `/issue verify`:
- `/issue verify <number> --dry-run` - Check status without modifying issue
- `/issue verify <number>` - Check status AND update issue checkboxes
- Exit condition: `/issue verify` reports "0 FAIL"

## When to Use

**Good for:**
- Well-defined issues with clear acceptance criteria
- Issues with multiple checkboxes to complete
- Iterative implementation work

**Not good for:**
- Issues requiring design decisions (use `/issue` create/update first)
- Issues without acceptance criteria
- Exploratory or research tasks
