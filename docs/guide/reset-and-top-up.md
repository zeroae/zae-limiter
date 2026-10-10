# Reset and Top-Up

Two administrative operations change an entity's balance on one resource **immediately**, on
every shard, without touching its configuration:

| Operation | What it does | Typical use |
|-----------|--------------|-------------|
| `Repository.reset_bucket()` | Restores each limit to its full share and starts a new period | Support gives a user their allowance back; a session is reset |
| `Repository.top_up()` | Adds tokens now | A purchase of more allowance; the difference after a plan upgrade |

```python
from datetime import timedelta

from zae_limiter import Limit, Repository

repo = await Repository.open()
await repo.set_limits(
    "user-123",
    [Limit.per_minute("rpm", 100), Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
    resource="claude",
)

# Everything back to full for this entity on this resource
await repo.reset_bucket("user-123", "claude")

# Only the session quota
await repo.reset_bucket("user-123", "claude", limits=["session"])

# 5,000 more session tokens, this period only
result = await repo.top_up("user-123", "claude", {"session": 5_000})
print(result.amounts)  # {'session': 5000}
```

The CLI equivalents are `zae-limiter entity reset` and `zae-limiter entity top-up` (see the
[CLI reference](../cli.md#reset-and-top-up)).

## What each operation does, per limit shape

| Limit shape | `reset_bucket()` | `top_up({name: N})` |
|-------------|------------------|---------------------|
| Dripping (`Limit.per_minute`, …) | Back to its ceiling; any debt is forgiven | At most the room below its ceiling — a refund, not a purchase. `result.amounts` says how much was granted |
| [Scheduled quota](scheduled-limits.md) (`Limit.quota(..., cron=...)`) | Back to its share; a new period starts now, and the next calendar reset still fires as usual | Exactly `N` for the current period, above the plan if need be. The next reset ends it |
| [Session quota](session-quotas.md) (`Limit.quota(..., reset_after=...)`) | Back to its share; the current window ends, so the next request opens a fresh one | Exactly `N` until the window ends. With no live window, the top-up opens one now: the purchase starts the session |

`amounts` mirrors `acquire(consume=...)`: limit name to whole tokens, and several limits can be
topped up in one call. A pending reset — a quota whose calendar edge or window end has passed
since it was last used — is applied first, in the same write, so the tokens always land in the
current period.

Neither operation touches the consumption counter (usage snapshots stay right), the `disabled`
flag, configuration, the entity's parent, or other resources. A cascading child's reset leaves
its parent's buckets alone; reset the parent too if it is the one exhausted.

## Plan upgrades and purchases

**A purchase** is `top_up()` alone. The tokens sit above the plan until the next reset or window
end; nothing persists into later periods.

**A plan upgrade** is two calls: write the new plan, then top up the difference so the change
is felt now rather than at the next reset (a quota never drips, so without the top-up the user
keeps the old remaining balance until then):

```python
await repo.set_limits(
    "user-123", [Limit.quota("daily", 25_000, cron="0 0 * * *")], resource="claude"
)
await repo.top_up("user-123", "claude", {"daily": 25_000 - 10_000})
```

The gap between the two calls can only under-admit.

## Requirements and costs

- **A quota top-up needs Lambdas at 0.17.0 or later.** An older aggregator would clamp the
  topped-up shard back to its plan and the purchase would be lost. `top_up()` checks the stack's
  version record once per repository and raises `VersionMismatchError` when it is older; it
  also raises the stack's minimum client version to 0.17.0. Run `zae-limiter upgrade` first.
  A reset, and a top-up of a dripping limit, are not gated.
- **Cost:** about 3 RCU + 2 WCU at one shard, growing by about 1 RCU + 2 WCU per extra shard
  (one transaction over every shard), plus one slow pass per shard on its next acquire. The
  acquire fast path is unchanged.
- **Another process may refuse for up to its rejection-cache TTL** (default 1 s) after a reset
  or top-up made elsewhere. The process that made the change admits at once.

## Edge cases

- A reset of an entity that has never acquired on the resource is a no-op (`result.shards ==
  0`): its first acquire creates the bucket at full share anyway.
- A quota top-up for an entity that has never acquired creates its bucket holding the full
  share plus the top-up. If the resource is disabled for the entity, it raises
  `ResourceDisabled` instead.
- A quota configured after the entity's bucket existed, and not yet used since, is granted
  nothing (`result.amounts` reports 0). Acquire once, then top up.
- If another writer keeps changing the bucket, the operation retries three times and then
  raises `RateLimiterUnavailable`; nothing was written.

See [ADR-149](../adr/149-reset-and-top-up.md) for the design.
