# Per-Resource Limits and a Shared Budget

A worked example that puts several features together. A multi-tenant service has many metered
resources (endpoints, products, regions — often 30 or more) and wants, for every user:

| Need | How | Section |
|------|-----|---------|
| Limits **per resource**, on more than one dimension (requests and units of work) | One resource per metered resource, several limits each | [Resources and the budget](#resources-and-the-budget) |
| A shared **budget** across every resource, each with its own price | A `budget` resource with a weekly and a session quota, debited in the same call | [Resources and the budget](#resources-and-the-budget) |
| **Plans** (free, pro, enterprise), with upgrades, purchases and session resets taking effect **at once** | Free plan as resource defaults, paid plans as entity config, `top_up()` and `reset_bucket()` | [Plans](#plans) |
| An **organisation** limit on the resources, but not on the budget | User → org parent, cascade on per resource | [Organisations](#organisations) |
| Usage **per organisation**, optionally capped in real time | Usage summaries; an opt-in cascade on the budget | [Roll-up per organisation](#roll-up-per-organisation) |
| A **non-enforcing** rollout | Soft limits and bypass | [Rolling out without enforcing](#rolling-out-without-enforcing) |

Each feature has its own page; this one shows how they fit and what a call costs.

## Resources and the budget

Give every metered resource its own resource name and its own limits. The budget is one more
resource, `budget`, holding two [quotas](scheduled-limits.md): a `weekly` allowance that comes
back every Monday, and a `session` allowance that comes back five hours after each user's own
first use ([session quotas](session-quotas.md)).

Budget amounts are integers, so pick a small unit: **micro-USD** below, so $5.00 is
`5_000_000`. The price table lives in your application; the limiter only sees the result.

```python
from datetime import timedelta

from zae_limiter import Limit

# The free plan: resource defaults, so every user without a plan of their own gets them
await limiter.set_resource_defaults(
    "search", [Limit.per_minute("requests", 60), Limit.per_minute("units", 10_000)]
)
await limiter.set_resource_defaults(
    "translate", [Limit.per_minute("requests", 30), Limit.per_minute("units", 5_000)]
)
await limiter.set_resource_defaults(
    "budget",
    [
        Limit.quota("weekly", 5_000_000, cron="0 0 * * 1"),  # $5 a week, back every Monday
        Limit.quota("session", 1_000_000, reset_after=timedelta(hours=5)),  # $1 a session
    ],
)

# Prices in micro-USD per request and per unit of work: application data, not limiter config
PRICES = {
    "search": {"requests": 2_000, "units": 50},
    "translate": {"requests": 1_000, "units": 20},
}


def cost(resource: str, usage: dict[str, int]) -> int:
    price = PRICES[resource]
    return sum(price[name] * amount for name, amount in usage.items())


await limiter.create_entity(entity_id="user-1")

estimate = {"requests": 1, "units": 100}
price = cost("search", estimate)  # 7,000 micro-USD
async with limiter.acquire(
    "user-1",
    "search",
    consume=estimate,
    also={"budget": {"weekly": price, "session": price}},
) as lease:
    actual = {"requests": 1, "units": 140}  # known once the work is done
    extra = cost("search", actual) - price
    await lease.adjust(units=actual["units"] - estimate["units"])  # search
    await lease.resource("budget").adjust(weekly=extra, session=extra)
    print(lease.resource("budget").consumed)  # {'weekly': 9000, 'session': 9000}
```

One `acquire()` debits the resource **and** the budget, and admits both or neither: if either
one is short, nothing is debited and `RateLimitExceeded` names the limit that was
([Several Resources per Acquire](multi-resource.md)). The two `adjust()` calls are written
together when the block exits, one write per bucket.

Both quotas take the same amount: every spend counts against the week and the session alike.

## Plans

The free plan is the resource defaults above. A paid plan is **entity config**: one
`set_limits()` per resource the plan changes, which outranks the resource defaults for that
user ([Configuration Hierarchy](config-hierarchy.md)). Keep the plan table in your
application, the same way as the prices:

```python
from datetime import timedelta

from zae_limiter import Limit

await limiter.set_resource_defaults(
    "budget",
    [
        Limit.quota("weekly", 5_000_000, cron="0 0 * * 1"),
        Limit.quota("session", 1_000_000, reset_after=timedelta(hours=5)),
    ],
)

PLANS = {
    "pro": {
        "search": [Limit.per_minute("requests", 600), Limit.per_minute("units", 100_000)],
        "budget": [
            Limit.quota("weekly", 50_000_000, cron="0 0 * * 1"),
            Limit.quota("session", 10_000_000, reset_after=timedelta(hours=5)),
        ],
    },
}


await limiter.create_entity(entity_id="user-2")

# Upgrade: the plan's limits on every resource it changes, then the quota difference
for resource, limits in PLANS["pro"].items():
    await limiter.set_limits("user-2", limits, resource=resource)
await repo.top_up(
    "user-2", "budget", {"weekly": 50_000_000 - 5_000_000, "session": 10_000_000 - 1_000_000}
)

# A purchase: $20 more this week; the order id makes a retry safe
await repo.top_up("user-2", "budget", {"weekly": 20_000_000}, idempotency_key="order-1001")

# Support resets the session
await repo.reset_bucket("user-2", "budget", limits=["session"])

# Downgrade: back to the resource defaults
for resource in PLANS["pro"]:
    await limiter.delete_limits("user-2", resource=resource)
availability = await limiter.check_availability("user-2", "budget")
print(availability.available["weekly"])  # 70000000: this week's top-ups last until Monday
```

Every change takes effect on the user's **next call**:

| Event | Calls | What the user sees |
|-------|-------|--------------------|
| Upgrade | `set_limits()` per resource, then `top_up()` the quota difference | New ceilings at once. A dripping limit refills toward its new ceiling at the new rate; a quota never drips, so without the top-up it would keep the old remaining balance until its next reset |
| Downgrade | `delete_limits()` per resource (back to free), or `set_limits()` to a smaller plan | Every balance is cut to the new ceiling on the next call. What was spent this period is not recounted: a user with $40 left on pro keeps $5, the free ceiling. **A top-up lasts until the quota's next reset, whatever the plan**, including the upgrade's own: the user in the example, upgraded and downgraded within one week, keeps $70 until Monday |
| Purchase | `top_up(..., idempotency_key=...)` | Exactly that much more, above the plan, until the quota's next reset |
| Session reset | `reset_bucket(..., limits=["session"])` | The session ends; the next call opens a fresh one at its full allowance |

`top_up()` and `reset_bucket()` change the balance on every shard in one transaction and record
an audit event; see [Reset and Top-Up](reset-and-top-up.md) for how each limit shape responds
and why a purchase should carry an idempotency key. A dripping limit gains at most its room
below the ceiling from `top_up()`; use `reset_bucket(..., limits=["requests"])` to fill one at
once.

!!! tip "Many users, few plans"
    Plans are set per user from your application. A [limits manifest](#a-manifest) suits the
    parts that change rarely — resource defaults and organisations — rather than thousands of
    users.

## Organisations

Make each organisation a parent entity and each user its child. A **cascade policy** per
resource decides where the organisation is debited too
([Cascade per Resource](hierarchical.md#cascade-per-resource)):

- the metered resources **cascade**: the organisation's limits protect a shared upstream quota;
- the `budget` **does not**: it is per user, and debiting the organisation would cost a write
  per call for nothing.

```python
await limiter.set_resource_defaults(
    "search", [Limit.per_minute("requests", 60), Limit.per_minute("units", 10_000)]
)
await limiter.set_resource_defaults("budget", [Limit.per_day("cents", 500)])

await limiter.create_entity(entity_id="org-acme")
await limiter.create_entity(entity_id="user-3", parent_id="org-acme")  # no cascade=True needed

# Metered resources count against the org; the budget stays per user
await repo.set_resource_cascade("search", True)
await repo.set_resource_cascade("budget", False)

# The org's own limits, wherever cascade is on: these are what a cascade debits.
# Only the shared upstream quota; requests stay a per-user limit.
await limiter.set_limits("org-acme", [Limit.per_day("units", 1_000_000)], resource="search")

async with limiter.acquire(
    "user-3", "search", consume={"requests": 1, "units": 100}, also={"budget": {"cents": 1}}
):
    pass

org = await limiter.check_availability("org-acme", "search")
print(org.available)  # about {'units': 999900}: debited on search, and only on units
```

The organisation is debited only on the limits it declares itself, so give it just the ones that
protect something shared.

!!! warning "The organisation needs its own limits wherever cascade is on"
    A cascade debits the parent against the parent's **own** limits for that resource. An
    organisation with none falls back to the resource defaults — one free user's numbers — and
    the whole organisation is then limited to one free user's allowance. Set them with
    `set_limits(org, ..., resource=...)` for every cascading resource, or in the manifest;
    `limits plan` warns when a manifest turns cascade on and no entity has its own limits there.

**Moving a user to another organisation** is one call, and takes effect on the user's next
call in every process ([Moving an Entity](hierarchical.md#moving-an-entity-to-a-new-parent)):

```python
await limiter.create_entity(entity_id="org-north")
await limiter.create_entity(entity_id="org-south")
await limiter.create_entity(entity_id="user-4", parent_id="org-north")

await limiter.set_parent("user-4", "org-south")
```

Usage already charged to the old organisation stays there.

## Roll-up per organisation

**Usage per organisation** comes from [usage snapshots](usage-snapshots.md), written by the
aggregator Lambda. On a cascading resource the organisation is debited on every call, so it has
snapshots of its own. The budget does not cascade, so add up the users':

```python
from zae_limiter import Limit

await limiter.create_entity(entity_id="org-acme")
await limiter.create_entity(entity_id="user-5", parent_id="org-acme")

# Cascading resource: the org's own snapshots
search = await limiter.get_usage_summary(entity_id="org-acme", resource="search")

# Per-user budget: the sum of the users' snapshots
weekly_spend = 0
for user in await limiter.get_children("org-acme"):
    summary = await limiter.get_usage_summary(entity_id=user.id, resource="budget")
    weekly_spend += summary.total.get("weekly", 0)
```

Snapshots are hourly and daily, and arrive after the aggregator processes the stream: good for
billing and dashboards, not for stopping a request.

**A real-time cap on the organisation's budget** is opt-in: turn cascade on for `budget` and
give the organisation budget limits of its own. Every call then also debits the organisation's
budget bucket, **+1 write at admission and +1 at reconcile** (+2 WCU per call):

```python
from zae_limiter import Limit

await limiter.set_resource_defaults("budget", [Limit.quota("weekly", 5_000_000, cron="0 0 * * 1")])
await limiter.create_entity(entity_id="org-acme")
await limiter.create_entity(entity_id="user-6", parent_id="org-acme")

await repo.set_resource_cascade("budget", True)
await limiter.set_limits(
    "org-acme", [Limit.quota("weekly", 500_000_000, cron="0 0 * * 1")], resource="budget"
)
```

The organisation's budget bucket is then written by every user, so read
[Gotchas](#gotchas) on hot organisation buckets first.

## Rolling out without enforcing

[Soft limits and bypass](soft-limits-and-bypass.md) let you meter before you enforce:

- **Soft** is per limit: introduce the budget with `soft=True`, watch who would have been
  rejected (`lease.resource("budget").overdrawn`, `UsageSnapshot.overdrawn`), then store it
  again without `soft` to enforce it. The other limits stay hard throughout.
- **Bypass** is per entity and resource: `bypass_entity("user-7", resource="budget")` waives
  one user's budget (an incident, a VIP), while their per-resource limits still apply.
  `clear_entity_disabled()` lifts it.

```python
from zae_limiter import Limit

await limiter.set_resource_defaults(
    "budget", [Limit.quota("weekly", 5_000_000, cron="0 0 * * 1", soft=True)]
)
await limiter.create_entity(entity_id="user-7")

async with limiter.acquire(
    "user-7", "search", consume={"rpm": 1}, also={"budget": {"weekly": 9_000_000}}
) as lease:
    print(lease.resource("budget").overdrawn)  # ['weekly']: admitted, and over the allowance
```

Both keep counting consumption, so usage snapshots stay exact.

## Cost per call

One call debits the resource for the user and the organisation, and the budget for the user —
three bucket items, written together on the fast path. Writes are $0.625 per million
([Performance](../performance.md)).

| Call | Admit | Reconcile (`adjust()` once usage is known) | Total | Per million calls |
|------|-------|--------------------------------------------|-------|-------------------|
| Resource cascades, budget per user | 3 WCU, 1 round trip | 3 WCU, 1 round trip | **6 WCU** | **$3.75** |
| …with a real-time organisation budget cap | 4 WCU | 4 WCU | **8 WCU** | **$5.00** |
| …when the estimate was exact (no adjust) | 3 WCU | 0 | 3 WCU | $1.88 |

No reads on the warm path: the fast path writes without reading configuration, and learns the
cascade policy and the parent from the bucket items its writes return. A user's first call (every resource on the slow path), a rejection,
and a rejection the client already knows about are priced in
[Several Resources per Acquire](multi-resource.md#cost).

## Gotchas

- **Organisation buckets are hot.** Every user's call writes the organisation's bucket on each
  cascading resource. When one bucket item takes more than about 1,000 writes a second it is
  split into shards automatically, up to 32
  ([Write Sharding](../performance.md#write-sharding-automatic-pre-shard-buckets)). Each shard
  holds `capacity // shard_count`, so **a single request larger than one shard's share is
  rejected** even while the organisation is under its limit: an organisation with
  `units: 1,000,000/day` at 32 shards admits at most 31,250 units in one call
  ([#475](https://github.com/zeroae/zae-limiter/issues/475)). Size organisation limits with
  that in mind, or split large requests.
- **Two levels only.** A user has one parent, which has none. To report on a level above the
  organisation (a region, a reseller), roll up the organisations' usage snapshots; it cannot
  also be enforced in the same call.
- **One resource per metered resource, not one item.** With 30 resources, do not fold them into
  one resource carrying 60 limits (`search_requests`, `translate_requests`, …). Every limit of a
  resource lives on one bucket item, so every call would write to one large item — a write to
  an item over 1 KB costs more than one WCU — and every call, whatever it used, would contend
  for that one item. A resource each means a call writes only what it uses.
- **At most 16 resources per call**, the primary included. A call that uses one metered
  resource and the budget needs two.
- **Limits are replaced, not merged.** `set_limits()` replaces a level's whole list, so a plan
  entry names every limit the plan sets for that resource
  ([Replacement Semantics](config-hierarchy.md#replacement-semantics)).
- **Version.** `top_up()`, `reset_bucket()`, `set_parent()` and soft limits need a stack whose
  Lambdas are 0.17.0 or later; run `zae-limiter upgrade` if a call is refused with
  `VersionMismatchError`.

## A manifest

The resource defaults and organisations change rarely, so they suit a
[limits manifest](../cli.md#declarative-limits) applied with `zae-limiter limits apply`. YAML
anchors keep 30 resources short: the provisioner reads only `namespace`, `system`, `resources`
and `entities`, so a top-level `x-…` key is a place to define them.

```yaml
namespace: default

x-shapes:
  standard-free: &standard-free
    requests: {capacity: 60}
    units: {capacity: 10000}
  standard-org: &standard-org   # the shared upstream quota only
    units: {capacity: 1000000, refill_period: 86400}

resources:
  search:
    cascade: true
    limits: *standard-free
  translate:
    cascade: true
    limits: *standard-free
  summarize:
    cascade: true
    limits:
      <<: *standard-free      # the standard shape, one limit changed
      units: {capacity: 2000}
  budget:
    cascade: false
    limits:
      weekly:
        capacity: 5000000
        reset_schedule: [{cron: "0 0 * * 1", tz: UTC}]
      session:
        capacity: 1000000
        reset_after_seconds: 18000

entities:
  org-acme:
    resources:
      search: {limits: *standard-org}
      translate: {limits: *standard-org}
      summarize: {limits: *standard-org}
```

`zae-limiter limits plan -f limits.yaml` shows what would change before you apply it. A paid
plan per user can be written the same way under `entities`, but with many users it is usually
simpler to call `set_limits()` from your application when the plan changes.

## Reference

| Task | Python | CLI |
|------|--------|-----|
| Free plan (resource defaults) | `limiter.set_resource_defaults()` | `zae-limiter resource set-defaults` |
| Paid plan (entity config) | `limiter.set_limits()` / `delete_limits()` | `zae-limiter entity set-limits` / `delete-limits` |
| Debit a resource and the budget | `limiter.acquire(..., also=...)`, `lease.resource()` | — (runtime only) |
| Purchase | `repo.top_up()` | [`zae-limiter entity top-up`](../cli.md#reset-and-top-up) |
| Reset a session | `repo.reset_bucket()` | [`zae-limiter entity reset`](../cli.md#reset-and-top-up) |
| Organisation | `limiter.create_entity(parent_id=...)` | `zae-limiter entity create --parent` |
| Cascade per resource | `repo.set_resource_cascade()`, `repo.set_entity_cascade()` | [`zae-limiter resource set-cascade`](../cli.md#cascade-policy-per-resource) |
| Move a user | `limiter.set_parent()` | [`zae-limiter entity set-parent`](../cli.md#moving-an-entity-to-a-new-parent) |
| Usage per organisation | `limiter.get_usage_summary()`, `limiter.get_children()` | `zae-limiter usage summary` |
| Soft limit | `Limit.*(..., soft=True)` | [`--soft NAME`](../cli.md#soft-limits_1) |
| Bypass | `repo.bypass_entity()`, `repo.bypass_resource()` | [`zae-limiter entity bypass`](../cli.md#bypass) |

The full signatures are in the [RateLimiter](../api/limiter.md) and
[Repository](../api/repository.md) API reference.
