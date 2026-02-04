# Write-on-Enter Invariant (Issue #309)

## Rule

The `acquire()` context manager MUST write initial token consumption to DynamoDB **before** yielding the lease. Tokens must be immediately visible to concurrent callers.

## Required Flow

```python
# CORRECT: write on enter
lease = await self._do_acquire(...)        # READ: fetch buckets, try_consume locally
await lease._commit_initial()              # WRITE: persist consumption to DynamoDB
try:
    yield lease                            # User code runs here
    await lease._commit_adjustments()      # WRITE: adjustment deltas (no-op if none)
except Exception:
    await lease._rollback()                # WRITE: compensating transaction
    raise
```

## Prohibited Pattern

```python
# WRONG: write on exit (creates phantom consumption window)
try:
    yield lease                            # User code runs with stale DynamoDB state
    await lease._commit()                  # Other callers over-admitted during this window
except Exception:
    await lease._rollback()                # No-op rollback (nothing was written)
    raise
```

## Why

With write-on-exit, there is a window between enter and exit where:
- Tokens appear consumed locally but are NOT consumed in DynamoDB
- Concurrent callers see stale (higher) token counts and may over-admit
- The window grows with the duration of work inside the context manager

## Key Invariants

1. `RateLimitExceeded` is raised BEFORE any DynamoDB write (no partial writes on rejection)
2. `_commit_initial()` writes all consumption (child + parent if cascade) atomically
3. `_commit_adjustments()` is a no-op when no `adjust()`, `consume()`, or `release()` calls were made
4. `_rollback()` writes a compensating transaction restoring only what `_commit_initial()` wrote
5. Rollback failure is logged but does not mask the original exception
