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
reads it on the server, so a change takes effect on the next request:

- **Entity level** (`set_limits`, `delete_limits`): every bucket of the entity is restamped by the
  existing parameter sync.
- **Resource and system level** (`set_resource_defaults`, `set_system_defaults` and their
  deletes): only when some limit's soft-ness actually changed, the buckets that level decides are
  restamped (an entity's own override is left alone). A system-level change reaches every bucket
  in the namespace.

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
the first enforced request.

**Cascade.** Each entity resolves its own mode. A bypassed child still debits, and can be
rejected by, its cascade parent; bypass the parent, or turn cascade off for that resource, when
the child must also skip the organisation's cap.

## Version requirements

Storing a soft limit or a bypass needs a stack whose Lambdas are 0.17.0 or later, and raises the
stack's minimum client version to 0.17.0. Older components fail safe — they **enforce**: an
older client ignores both stamps, and an older reader of the config sees a bypass as an explicit
enforce.
