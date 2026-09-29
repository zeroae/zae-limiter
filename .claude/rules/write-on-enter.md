# Write-on-Enter Invariant (Issue #309)

## Rule

The `acquire()` context manager MUST write initial token consumption to DynamoDB **before** yielding the lease. Tokens must be immediately visible to concurrent callers.

## Required Flow

```python
# CORRECT: write on enter
lease = await self._do_acquire(...)  # READ: fetch buckets, try_consume locally
await lease._commit_initial()  # WRITE: persist consumption via transact_write()
try:
    yield lease  # User code runs here
    await lease._commit_adjustments()  # WRITE: adjustment deltas via write_each() (no-op if none)
except Exception:
    await lease._rollback()  # WRITE: compensating deltas via write_each()
    raise
```

## Prohibited Pattern

```python
# WRONG: write on exit (creates phantom consumption window)
try:
    yield lease  # User code runs with stale DynamoDB state
    await lease._commit()  # Other callers over-admitted during this window
except Exception:
    await lease._rollback()  # No-op rollback (nothing was written)
    raise
```

## Why

With write-on-exit, there is a window between enter and exit where:
- Tokens appear consumed locally but are NOT consumed in DynamoDB
- Concurrent callers see stale (higher) token counts and may over-admit
- The window grows with the duration of work inside the context manager

## Key Invariants

1. `RateLimitExceeded` is raised BEFORE any write of **consumption** — nothing a rejected request asked for is ever debited. Two documented writes may precede a rejection, and neither admits anything. The first is the **ADR-145 quota move**, committed with nothing consumed: a pass that creates or seeds a quota shard whose slot a sibling covers plans a move off that sibling; if the acquire is then rejected, `RateLimiter._commit_rejected_moves` undoes every in-memory debit and commits the same transaction (recipient plus donor debit, every `consumed = 0`) so the moved tokens are not lost, then raises. If that commit fails, nothing was written (the transaction is atomic) and the rejection stands. The second is planning's own **count raise** (`Repository._freeze_and_raise_shard_counts`, design §8 R5): before a fresh grant, a sibling whose stored `shard_count` lags is raised to the planned count, freezing a legacy grant size onto it — no `tk` moves. The same freeze also runs after that move commit when it created a shard a racing doubling overtook (`Repository.repair_created_quota_shard`, design §8 R7): a count raise on the new shard, no `tk`. The pre-ADR-145 writes — the #587 reclaim clamp and the #633 `persist_seed` — no longer exist
2. `_commit_initial()` writes all consumption (child + parent if cascade) atomically via `transact_write()`
3. `_commit_adjustments()` is a no-op when no `adjust()`, `consume()`, or `release()` calls were made
4. `_commit_adjustments()` and `_rollback()` use `write_each()` (independent single-item writes, 1 WCU each) since they produce unconditional ADD operations that do not require cross-item atomicity
5. `_rollback()` restores only what `_commit_initial()` wrote
6. Rollback failure is logged but does not mask the original exception
