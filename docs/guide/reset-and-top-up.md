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
| [Session quota](session-quotas.md) (`Limit.quota(..., reset_after=...)`) | Its current window ends; the next request opens a fresh one at its full share (the balance is left for that request to restore) | Exactly `N` until the window ends. With no live window, the top-up opens one now: the purchase starts the session |

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

The gap between the two calls can only under-admit. The top-up still records the whole
difference as topped up (`b_{name}_tu`), so the upgrade needs the 0.17.0 Lambdas below, and
until the next reset the quota's ceiling is up to one top-up wider than the new plan. That only
lets refunds of tokens already spent land; it never grants extra.

## Retries: pass an idempotency key

**Without a key, an error from `reset_bucket()` or `top_up()` leaves the outcome unknown**: a
timeout can arrive after the write landed, and retrying may grant twice. Name the operation
with something unique to it — a purchase or ticket id — and retry with the same key:

```python
await repo.set_limits(
    "user-123", [Limit.quota("daily", 10_000, cron="0 0 * * *")], resource="claude"
)
result = await repo.top_up("user-123", "claude", {"daily": 5_000}, idempotency_key="order-8841")
# A retry within three days returns the same result and grants nothing again.
again = await repo.top_up("user-123", "claude", {"daily": 5_000}, idempotency_key="order-8841")
assert again == result
```

A key is 1-64 characters of letters, digits, `.`, `_`, `:` and `-`, and names one kind of
operation: reusing a reset's key for a top-up is a `ValidationError`. With a key, each call
reads its record first (+1 RCU).

A keyed call that changes nothing is recorded too: a reset of an entity with no bucket yet, or
a top-up of dripping limits only. A retry with the same key returns that first result (0)
even after the bucket exists, and grants nothing.

A key belongs to one entity, and to one *incarnation* of it: if the entity is deleted and
created again, the same key names a new operation. Two cases are not covered:

| Case | What happens |
|------|--------------|
| The entity is deleted and recreated *while* a call is in flight | A retry with the same key can be applied once more |
| A call made while the entity has no entity record, or a key first used then (for example, right after `delete_entity`, or before `create_entity`) | It can return the earlier result and apply nothing, even after the entity is created again |

The second applies too little, never too much. To be safe, use a fresh key after deleting an
entity, and once the entity exists.

## Requirements and costs

- **A quota top-up needs Lambdas at 0.17.0 or later.** An older aggregator would clamp the
  topped-up shard back to its plan and the purchase would be lost. `top_up()` checks the stack's
  version record once per repository and raises `VersionMismatchError` when it is older; it
  also raises the stack's minimum client version to 0.17.0. Run `zae-limiter upgrade` first.
  A reset, and a top-up of a dripping limit, are not gated.
- **Cost:** about 3 RCU + 6 WCU at one shard (one transaction holding the shard write, the
  operation's own record and its audit event), growing by about 1 RCU + 2 WCU per extra
  shard, plus one slow pass per shard on its next acquire. A keyed call that changes nothing
  costs 4 WCU (its record and audit event); without a key it writes nothing. The acquire fast
  path is unchanged.
- **Another process may refuse for up to its rejection-cache TTL** (default 1 s) after a reset
  or top-up made elsewhere. The process that made the change admits at once.

## Who may reset or top up

Reset and top-up are **operator actions**: call them from a principal holding the `full` or
`ns-full` policy (or your own equivalent), never from the application that is being limited.

!!! warning "IAM cannot stop an application from granting itself allowance"
    A principal that may `acquire()` must be able to update its own bucket items, so it can also
    raise their balance, or a quota's top-up, with a direct DynamoDB write, and the stack's
    acquire policies (`acq`, `ns-acq`) do not refuse `reset_bucket()` or `top_up()` either. An
    application enforces its own limits; it can also simply not call `acquire()`. When paid
    allowance must hold even against the application, the application must hold no DynamoDB
    write credentials: run `acquire()` in a service it can only invoke, and top-ups in an
    operator-only one.

## Edge cases

- A reset of an entity that has never acquired on the resource is a no-op (`result.shards ==
  0`): its first acquire creates the bucket at full share anyway.
- A quota top-up for an entity that has never acquired creates its bucket holding the full
  share plus the top-up. If the resource is disabled for the entity, it raises
  `ResourceDisabled` instead.
- A limit configured after the entity's bucket existed, and not yet used since, is not on
  the bucket yet: a top-up of it raises a `ValidationError` naming it, and writes nothing.
  Acquire once, then top up.
- If another writer keeps changing the bucket, the operation retries three times and then
  raises `RateLimiterUnavailable`. The reset or top-up itself was not applied. One write can
  remain: when the bucket's shards disagree on their shard count, the lagging shards are first
  raised to the highest count (bookkeeping that moves no tokens), and that raise is kept.
- A write whose response is lost is never applied twice: each call writes its own operation
  record in the same transaction, and a retry that finds it returns the stored result instead
  of re-applying — even when another reset or top-up landed on the bucket in between.
- Every operation that writes has exactly one audit event: it is written in the same
  transaction, so it lands exactly when the operation does, however the call ends. An invalid
  `principal` is refused before anything is written.
- On a bucket using resource or system defaults, which expires when idle, a reset or top-up
  also extends the bucket's expiry to cover at least the current period, so a purchase is not
  swept away before it can be spent. A bucket with entity-level limits never expires.

See [ADR-149](../adr/149-reset-and-top-up.md) for the design.
