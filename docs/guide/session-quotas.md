# Session Quotas

A session quota is an allowance that comes back a fixed time after **each caller's own first
use** — "10,000 tokens, and your window resets five hours after you started" — rather than at
the same wall-clock instant for everyone.

```python
from datetime import timedelta

from zae_limiter import Limit

session = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
```

Two callers that first use it at 09:00 and 14:30 get windows ending at 14:00 and 19:30.

!!! warning "Upgrade every client and the aggregator before storing one"
    `reset_after` is new in v0.15.0, and nothing checks versions for you. A client older than
    v0.15.0 cannot read a level that stores a `reset_after` limit: with
    `on_unavailable="block"` every `acquire()` against that level raises
    `RateLimiterUnavailable`, and with `on_unavailable="allow"` it is admitted **with no
    limiting at all**, including the other limits on that level. An older aggregator Lambda
    treats the quota as a dripping limit and grants each shard it pre-creates a fresh share.

## Which one do I want?

`Limit.quota()` takes exactly one of `cron` or `reset_after`. Both build a **quota** — a limit
that does not drip and hands back its whole allowance in one lump — and differ only in *when*.

| You want | Use | Resets | Decided in |
|----------|-----|--------|------------|
| A billing period: "10,000 per calendar month" | `cron="0 0 1 * *"` | At the same wall-clock instant for every caller | [ADR-138](../adr/138-fixed-reset-windows-only.md) |
| A session cap: "10,000 per five hours of use" | `reset_after=timedelta(hours=5)` | A fixed time after **each caller's own** first use | [ADR-139](../adr/139-duration-reset-windows.md) |

You cannot have both on one limit. A limit has one recovery mechanism
([ADR-137](../adr/137-reset-replaces-drip.md)): a quota that reset at midnight *and* five hours
after first use would hand back its allowance twice over some periods and once over others, and
there would be no single number that is "the allowance". Passing both, or neither, raises
`ValueError`:

```python
from datetime import timedelta

from zae_limiter import Limit

try:
    Limit.quota("session", 10_000, cron="0 0 * * *", reset_after=timedelta(hours=5))
except ValueError as exc:
    print(exc)
```

Calendar quotas are covered in [Scheduled (Cron) Limits](scheduled-limits.md#quotas).

## Setting one

A session quota is stored like any other limit and resolves through the normal
[configuration hierarchy](config-hierarchy.md):

```python
from datetime import timedelta

from zae_limiter import Limit, RateLimiter, Repository

repo = await Repository.open()
limiter = RateLimiter(repository=repo)

await limiter.set_limits(
    "user-123",
    limits=[
        Limit.quota("session", 10_000, reset_after=timedelta(hours=5)),
        Limit.per_minute("rpm", 60),
    ],
    resource="claude-sonnet",
)

async with limiter.acquire("user-123", "claude-sonnet", consume={"session": 1_500, "rpm": 1}):
    ...  # the window opened here, at this request
```

A quota can sit beside ordinary rate limits on the same resource; each recovers its own way.

`reset_after` must be a positive whole number of seconds, at most 10⁹ (the same ceiling every
duration on a limit shares). A fractional second is rejected rather than rounded.

## Idle-restarting, not tiling

A window does not run forward forever from the first-ever call. When a window ends while the
caller is quiet, it is simply over, and the **next** admitted request opens a fresh one at that
instant.

```mermaid
gantt
    title  reset_after = 5h, one caller
    dateFormat HH:mm
    axisFormat %H:%M
    section Windows
    window 1 (first use 09:00)   :active, w1, 09:00, 14:00
    idle                          :done, i1, 14:00, 16:30
    window 2 (next use 16:30)    :active, w2, 16:30, 21:30
```

So a caller *can* reset its own window by going idle past the end of it, and that is intended —
it is what "five hours from when you started" means. It is also why a swept bucket is harmless:
see [Limitations](#limitations).

## What opens a window

Only **admitted** use. A window opens on the first request after the previous one ended that
is actually admitted and written.

A caller hammering an exhausted quota does not keep restarting its own five hours. Inside a
window, an exhausted quota's rejection writes nothing — on the fast path it is a free rejection,
and on the slow path `RateLimitExceeded` is raised before any write — so the anchor never moves
until the window it already opened has ended.

## Cascade

A parent and its children anchor their windows **independently**. The parent's window opens at
the first admitted request that debits the parent — whichever child (or the parent itself) made
it — and runs its own course from there. It does not move when a child's own window restarts,
and a child's window does not move when the parent's does.

```python
from datetime import timedelta

from zae_limiter import Limit

await limiter.create_entity("org-acme")
await limiter.create_entity("user-alice", parent_id="org-acme", cascade=True)

await limiter.set_limits(
    "org-acme",
    limits=[Limit.quota("session", 100_000, reset_after=timedelta(hours=5))],
    resource="claude-sonnet",
)
await limiter.set_limits(
    "user-alice",
    limits=[Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
    resource="claude-sonnet",
)

# Debits both, against two separate windows.
async with limiter.acquire("user-alice", "claude-sonnet", consume={"session": 500}):
    ...
```

## What it costs

| Event | Extra DynamoDB cost |
|-------|---------------------|
| An acquire inside a window | **None.** The fast path is byte-identical to any other limit: 1 WCU, 0 reads |
| A new window opening | One slow-path request **per shard** (S in all, as at any [schedule boundary](scheduled-limits.md#what-happens-at-a-boundary)), plus **(S − 1) × L** conditional writes by the request that opened it, where S is the entity's shard count and L the number of `reset_after` limits on the bucket |
| An unsharded entity (S = 1) opening a window | No extra writes at all |
| A new shard being created mid-window | One strongly consistent read of shard 0 (1 RCU), once per shard |

The extra writes exist because a sharded entity's shards must agree on one window. Without
them, "when does my quota reset" would have no honest answer. See
[Performance](../performance.md#session-quotas) for the full breakdown.

## What a 429 tells the caller

A rejected quota reports `kind: "quota"`, its `capacity`, and `resets_at_ms` — the **absolute**
epoch-millisecond instant the window ends and the allowance returns. A client can schedule a
retry against it without parsing anything:

```python
from datetime import timedelta

from zae_limiter import Limit, RateLimitExceeded

await limiter.set_limits(
    "user-123",
    limits=[Limit.quota("session", 1_000, reset_after=timedelta(hours=5))],
    resource="claude-haiku",
)

async with limiter.acquire("user-123", "claude-haiku", consume={"session": 1_000}):
    pass  # spends the whole allowance

try:
    async with limiter.acquire("user-123", "claude-haiku", consume={"session": 1}):
        pass
except RateLimitExceeded as exc:
    body = exc.as_dict()
    session = body["limits"][0]
    assert session["kind"] == "quota"
    assert session["capacity"] == 1_000
    assert session["resets_at_ms"] is not None  # window end, epoch ms
    assert "refill_amount" not in session  # a quota does not drip
```

`retry_after_seconds` on the same exception is the wait until that instant.

For a display — "8,500 left · resets at 14:00" — use `check_availability()`, which reads every
shard once and never writes:

```python
from datetime import timedelta

from zae_limiter import Limit

await limiter.set_limits(
    "user-123",
    limits=[Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
    resource="claude-sonnet",
)
async with limiter.acquire("user-123", "claude-sonnet", consume={"session": 1_500}):
    pass

availability = await limiter.check_availability("user-123", "claude-sonnet")
status = availability.status("session")
assert status.available == 8_500
assert status.resets_at_ms is not None  # five hours after the acquire above
```

`resets_at_ms` is `None` when no window is live — the caller has not used the quota yet, or its
last window has ended and the next request will open a fresh one at full allowance. It is never
an instant in the past.

## Viewing and declaring one

The CLI renders a session quota on one line, and says "after first use" so it is not mistaken
for a clock-aligned period:

```console
$ zae-limiter entity get-limits user-123 --resource claude-sonnet
Limits for entity 'user-123' on resource 'claude-sonnet':
  session: 10,000 quota (resets 5h after first use)
  rpm: 60/min
```

The `-l name:rate/period` flag **cannot** express `reset_after`, and a set replaces the whole
level — so `entity set-limits ... -l session:10000/hour` would turn a stored session quota into a
dripping limit. Use a manifest or the Python API. In a YAML manifest the length is a whole
number of seconds, and `refill_amount` defaults to `0`:

```yaml
namespace: default
resources:
  claude-sonnet:
    limits:
      session:
        capacity: 10000
        reset_after_seconds: 18000   # 5h from each entity's own first use
```

It round-trips through the `Custom::ZaeLimiterLimits` CloudFormation resource as
`ResetAfterSeconds`.

## Limitations

- **A single request larger than one shard's share is unadmittable** while the entity is under
  its configured quota. A heavily used entity is split across up to 32 shards, each holding
  `capacity // shard_count`, and one request must fit on one shard. Inherited from the sharding
  design ([#475](https://github.com/zeroae/zae-limiter/issues/475)); keep single requests well
  below `capacity / 32` for entities that may shard.
- **Shards can disagree by a few milliseconds.** When two requests open a new window on two
  shards at almost the same instant, each keeps its own start until the next window. The entity
  still admits at most one allowance per window, and `check_availability()` reports the later
  end. A write that fails to reach a shard leaves it staggered the same way until it next rolls.
- **Windows run on the clocks of the clients that open them.** Clock skew between your
  application hosts shifts a window's end by up to that skew.
- **Resource- and system-level session quotas expire with their bucket.** A bucket configured
  from resource or system defaults expires after `reset_after × 7` idle (the default
  [TTL multiplier](../operations/rate-limits.md#bucket-expiry)) — 35 hours for a 5-hour window.
  The next request then opens a fresh window, which is exactly what idle-restarting specifies.
  Entity-level limits never expire.
- **The CLI's `-l` flag cannot set one** (above).

## See also

- [Scheduled (Cron) Limits](scheduled-limits.md) — calendar quotas and time-of-day windows
- [ADR-139](../adr/139-duration-reset-windows.md) — the design record, including the
  cross-shard mechanism
- [Basic Usage](basic-usage.md) — `acquire()`, leases, and handling `RateLimitExceeded`
