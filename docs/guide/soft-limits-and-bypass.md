# Soft Limits and Bypass

Two ways to run a limit without enforcing it. Both keep counting consumption, so usage
snapshots stay exact. See [ADR-151](../adr/151-non-enforcing-limits.md) for the decision record.

| | Soft limit | Bypass |
|---|---|---|
| What it is | A property of **one limit** | An access mode of an **entity on a resource** |
| Admits? | Always, for that limit — other, hard limits still gate | Always — only the reserved `wcu` write limit still gates |
| Balance | Debited, may go into debt | **Untouched** |
| Consumption (`tc`, usage snapshots) | Counted | Counted |
| Set with | `Limit.per_minute("tpm", …, soft=True)`, `--soft tpm`, `soft: true` | `bypass_resource()` / `bypass_entity()`, `resource bypass`, `disabled: bypass` |
| Fast-path cost | 0 RCU + 1 WCU, unchanged | 0 RCU + 1 WCU once the process has seen the bypass |
| Extra cost | One renewal per bucket shard per `stamp_ttl` ([below](#stamps-expire)) | Same |

## Soft limits

A soft limit is metered exactly like a hard one — every admitted request debits it and it may go
into debt — but it is never a reason to reject. Typical uses:

- **Metering for billing** beside a hard limit: a soft `tpm` that records how far a tenant went
  over, next to a hard `rpm` that protects the service.
- **Shadow rollout** of a new limit: configure it soft, watch who would have been rejected, then
  make it hard.

```{.python .lint-only}
from zae_limiter import Limit

await repo.set_resource_defaults(
    "gpt-4",
    [
        Limit.per_minute("rpm", 500),                # hard: still rejects
        Limit.per_minute("tpm", 50_000, soft=True),  # soft: metered only
    ],
)

async with limiter.acquire("user-1", "gpt-4", consume={"rpm": 1, "tpm": 80_000}) as lease:
    if lease.overdrawn:  # ["tpm"]: the soft limit is in debt on this shard
        print("over the metered allowance:", lease.overdrawn)
```

Every factory takes `soft=`: `per_second`, `per_minute`, `per_hour`, `per_day`, `custom` and
`quota`.

**Soft-ness belongs to the limit.** It resolves with the limit through the
[configuration hierarchy](config-hierarchy.md): an entity that redefines `tpm` redefines whether
it is soft too. Omitting `soft=True` on an entity-level `tpm` makes that entity's `tpm` hard,
whatever the resource level says.

### What callers see

- `RateLimitExceeded` lists a soft limit among `passed`, never among `violations`, and its
  `retry_after_seconds` never contributes to the overall retry hint. Every entry of `as_dict()`
  carries `"soft": true|false`.
- `LimitStatus.soft` and the derived `LimitStatus.overdrawn` (`soft` and `available < 0`).
- `Lease.overdrawn` — the declared soft limits whose balance is below zero after the admission
  write, on the shard written. Free: it is read off the write's own result.
- `check_availability()` reports a soft limit's real (possibly negative) balance; it never makes
  `allowed` false.
- Usage snapshots carry `UsageSnapshot.overdrawn`: per soft limit, the number of requests in the
  window that left a shard in debt. The aggregator counts it in the same snapshot write, at no
  extra cost.

### Quotas and sessions

A soft [quota](scheduled-limits.md) is debited and may go into debt within its period; the reset
restores the allowance and **forgives the debt** — the overdraw counter keeps the record. A soft
admission anchors a [session window](session-quotas.md) like a hard one.

### How a change takes effect

Bucket items carry a per-limit `soft` stamp, and the conditional write that admits a request
reads it on the server, so a change takes effect on the next request. The stamp is a deadline
that the next request past it renews or removes from config (see
[Stamps expire](#stamps-expire)), so even a change the restamping below misses takes effect
within `stamp_ttl`:

- **Entity level** (`set_limits`, `delete_limits`): every bucket of the entity is restamped by the
  existing parameter sync.
- **Resource and system level** (`set_resource_defaults`, `set_system_defaults` and their
  deletes): only when some limit's soft-ness actually changed, the buckets that level decides are
  restamped (an entity's own override is left alone). A system-level change reaches every bucket
  in the namespace.

A soft limit passed to one call, `acquire(..., limits=[...])`, is soft for that call only: it is
not config, so it stamps no bucket and other callers keep the stored definition. Such a call takes
the slow path (a read before the write) whenever it consumes a limit it declares soft.

## Bypass

Bypass waives every limit for one entity, one resource, or one entity on one resource. Requests
are admitted, no balance is debited, and consumption is still counted. Only the reserved `wcu`
limit — which protects the DynamoDB partition, not the tenant — still gates.

```{.python .lint-only}
await repo.bypass_resource("gpt-4")              # every entity without its own value
await repo.bypass_entity("vip-1")                # one entity, every resource
await repo.bypass_entity("vip-1", resource="gpt-4")

await repo.clear_resource_disabled("gpt-4")      # lift it

async with limiter.acquire("vip-1", "gpt-4", consume={"rpm": 1}) as lease:
    assert lease.bypassed
```

Bypass is the fourth value of the [`disabled`](basic-usage.md#disabling-resources-and-entities)
setting, so it follows the same walk — entity (resource-specific) → entity (`_default_`) →
resource, first explicit value wins:

| Resource | Entity | Result for the entity |
|---|---|---|
| bypass | — | bypassed |
| disabled | bypass | bypassed (a carve-out) |
| bypass | disabled | `ResourceDisabled` |
| bypass | enabled (`false`) | enforced |

`set_resource_defaults(..., disabled="bypass")` and `set_limits(..., disabled="bypass")` set it
too; `get_resource_disabled()` and `get_entity_disabled()` return `"bypass"`.

Lifting a bypass needs no reset: nothing was debited while it stood, so the entity resumes with
the balance it had. A calendar reset or session window that fell due while bypassed is applied by
the first enforced request. Lifting it reaches existing buckets at once through the same eager
restamping `disabled` uses, and at the latest within `stamp_ttl` for any bucket that restamping
missed (see [Stamps expire](#stamps-expire)).

**Cascade.** Each entity resolves its own mode. A bypassed child still debits, and can be
rejected by, its cascade parent; bypass the parent, or turn cascade off for that resource, when
the child must also skip the organisation's cap.

## Stamps expire

Both settings reach existing buckets as a stamp on each bucket item, which the admitting write
checks on the server. A change of setting restamps every existing bucket at once, but that
restamping can miss one: a bucket created by a request already in flight, a copy the aggregator
made of an older bucket, or a restamping that failed part-way. So every stamp carries a
**deadline**, at most `stamp_ttl` seconds (default 60) after the configuration read that
justified it:

- Before the deadline the stamp is honoured, with no configuration read: the request costs what
  any request costs.
- The first request after the deadline takes the slow path, which reads the configuration
  (bypassing the configuration cache) and renews the stamp — or removes it, when the setting has
  changed. That costs about one extra failed write, 2.5 read units and one more write, once per
  bucket shard per `stamp_ttl`: roughly +$0.016 per million requests at 1 request a second on
  the bucket, and about +0.15% at 100–1,000 requests a second.
- A bucket the restamping missed therefore keeps its old setting for at most `stamp_ttl` (plus
  clock skew between your processes). A bucket missing a stamp it should carry enforces, which
  only ever under-admits, until its next slow-path request or restamping.

`stamp_ttl` is set by the process that writes the stamps — the one running `acquire()` and the
one making the change:

```{.python .lint-only}
repo = await Repository.open("my-app", stamp_ttl=30)          # also Repository.connect()
repo = await Repository.builder().stamp_ttl(30).build()
```

It must be at least 10 seconds. A shorter `stamp_ttl` bounds how long a missed change lasts; a
longer one renews less often. The provisioner Lambda (`zae-limiter limits apply`) uses 60
seconds.

## Version requirements

Storing a soft limit or a bypass needs a stack whose Lambdas are 0.17.0 or later, and raises the
stack's minimum client version to 0.17.0. Older components fail safe — they **enforce**: an
older client ignores both stamps, and an older reader of the config sees a bypass as an explicit
enforce.
