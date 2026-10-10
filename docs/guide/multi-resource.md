# Several Resources in One Acquire

Some requests spend more than one resource. A search call is metered on `search` (requests
and units, cascading to the organisation) **and** draws on a per-user `budget` (weekly and
session quotas). Without help that takes two acquires: two round trips one after the other,
two leases, two reconciles, and a hand-written refund whenever the second one rejects after
the first has already debited.

`acquire()` takes the extra resources in `also`, and admits **all of them or none**.

## Acquiring

```python
await limiter.create_entity(entity_id="user-675")
await limiter.set_resource_defaults("search", [Limit.per_minute("rpm", 100)])
await limiter.set_resource_defaults("budget", [Limit.per_day("units", 10_000)])

async with limiter.acquire(
    "user-675",
    "search",
    consume={"rpm": 1},
    also={"budget": {"units": 50}},
) as lease:
    print(lease.resources)  # ('search', 'budget')
```

The `resource` argument is the **primary** resource. Each resource in `also` maps to its own
amounts by limit name, exactly like `consume`. Every resource resolves its own limits, its own
`disabled` flag, its own [cascade policy](hierarchical.md#cascade-per-resource), its own
shards and its own quota grants — just as if it had been acquired alone.

## All or none

When **any** resource is rejected, disabled or unavailable, every debit the acquire already
wrote is refunded **before** the exception reaches you. You never write a refund by hand.

| What happened | You get |
|---------------|---------|
| Every resource admitted | One lease covering all of them |
| Any resource over its limit | `RateLimitExceeded`, every debit refunded |
| Any resource disabled | `ResourceDisabled` (outranks `RateLimitExceeded`), every debit refunded |
| DynamoDB error | Refunds, then `on_unavailable` as usual: `RateLimiterUnavailable`, or a no-op lease under `ALLOW` |

```python
await limiter.create_entity(entity_id="user-676")
await limiter.set_resource_defaults("search", [Limit.per_minute("rpm", 100)])
await limiter.set_resource_defaults("budget", [Limit.per_day("units", 100)])

try:
    async with limiter.acquire(
        "user-676", "search", consume={"rpm": 1}, also={"budget": {"units": 500}}
    ):
        pass
except RateLimitExceeded as e:
    for status in e.violations:
        print(status.resource, status.limit_name)  # budget units
    print(e.retry_after_seconds)  # until every resource can admit
```

`RateLimitExceeded.statuses` carries a status for every declared limit of every resource the
decision looked at, each tagged with its `resource` (also in `as_dict()`). A resource the
limiter never got to evaluate — because another one was already known to reject — is absent.
`retry_after_seconds` is the wait until **every** resource can admit.

## Adjusting each resource

`lease.adjust()`, `consume()`, `release()` and `consumed` act on the **primary** resource,
so code written for one resource keeps working unchanged. Reach every other resource through
`lease.resource(name)`:

```python
await limiter.create_entity(entity_id="user-677")
await limiter.set_resource_defaults("search", [Limit.per_minute("units", 1_000)])
await limiter.set_resource_defaults("budget", [Limit.per_day("units", 10_000)])

estimate = 50
async with limiter.acquire(
    "user-677",
    "search",
    consume={"units": estimate},
    also={"budget": {"units": estimate}},
) as lease:
    actual = 80  # known once the work is done
    await lease.adjust(units=actual - estimate)  # search
    await lease.resource("budget").adjust(units=actual - estimate)
    print(lease.resource("budget").consumed)  # {'units': 80}
```

Two resources may declare limits with the same name (`units` above); each handle sees only
its own. Nothing is written until the context exits, where every resource is reconciled in
one go — one write per bucket, issued together. An exception inside the block refunds every
resource.

`lease.resource()` raises `ValidationError` for a resource the lease does not cover, so a typo
is never silent. On the no-op lease yielded under `on_unavailable=ALLOW`, every name returns a
no-op handle.

## Rules

- At most **16** resources per acquire, the primary included
  (`zae_limiter.limiter.MAX_ACQUIRE_RESOURCES`).
- A resource appears once: naming the primary resource in `also` is a `ValidationError`.
- `limits=` cannot be combined with `also`: a list of limits names no resource, so it cannot
  say which one it overrides. Store the limits instead
  ([Configuration Hierarchy](config-hierarchy.md)).
- An empty `also` is an ordinary single-resource acquire.

All of these are checked before anything is read or written.

## Cost

Each resource costs what it costs alone — one write per bucket item. What changes is latency:
the fast-path writes for every resource go out **together**, in one round trip.

The example from the top of the page — `search` cascading to the organisation, `budget` per
user — on warm caches:

| Case | Round trips | RCU | WCU |
|------|-------------|-----|-----|
| All admitted | **1** (2 with two acquires) | 0 | 3 |
| Exit reconcile, every resource adjusted | 1 | 0 | 3 |
| Rejected, and the [rejection cache](../performance.md) already knows | 0 | 0 | 0 |
| `budget` rejected, cache cold | 2 | 0 | 3 + 2 refunds |
| First acquire of the entity (every resource on the slow path) | ≈4 | ≈2.5 | 8 (one 3-item transaction + 2 failed writes) |

A cold rejection costs more than two hand-ordered acquires would, because the writes went out
together and the ones that landed are refunded. Repeat rejections within the rejection cache's
window cost nothing.

!!! note "Briefly visible debits"
    On the fast path a debit can be visible to other callers for up to two round trips before
    it is refunded. That can only make them reject a little early; it never admits anything
    extra.

See [ADR-148](../adr/148-multi-resource-acquire.md) for the full design.
