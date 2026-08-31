# ADR-125: Resource and Entity Disable

**Status:** Proposed
**Date:** 2026-08-30
**Issue:** TBD — link the tracking issue once filed

## Context

Operators need to turn a resource off, at the resource level and per entity, without
deleting its limit configuration. Two designs were considered: a `disabled` flag stored
beside `limits`, and reusing a limit with `capacity: 0`.

`capacity: 0` does not work. `acquire()` defaults to `speculative_writes=True`, whose
admission test is a conditional `UpdateItem` on the bucket item with
`b_{limit}_tk >= :consumed` — tokens, not capacity, and no config read at all.
`_sync_bucket_params` (ADR-120) propagates changed `cp`/`ra`/`rp` to an existing bucket
but never resets `tk`, and it only fires for entity-level `set_limits`/`delete_limits`
— `set_resource_defaults` and `set_system_defaults` have no fan-out. A bucket holding a
balance therefore keeps admitting traffic against a "disabled" resource. `capacity: 0`
would also require loosening `Limit.__post_init__`'s positive-only invariants, destroys
the configured capacity it overwrites, and surfaces a permanently-off resource as a
retryable 429 with a finite `retry_after_seconds` that never comes good.

## Decision

1. Store `disabled` as a **tri-state** attribute on existing config items: absent means
   inherit, `true`/`false` are explicit. Config items already carry non-limit siblings
   (`on_unavailable`, `resource`, `entity_id`), and `_deserialize_composite_limits`
   discovers limits by scanning for `l_*_cp`, so a sibling attribute is invisible to it.

2. Resolve `disabled` by an **independent walk** over entity(resource) →
   entity(`_default_`) → resource. First explicit value wins, regardless of whether that
   level defines limits. This is what makes an entity-level `disabled: false` re-admit a
   specific entity to a disabled resource.

3. **Denormalize** the resolved value onto bucket items and enforce it by adding
   `attribute_not_exists(#disabled)` to the speculative `ConditionExpression`. Bucket
   items already denormalize `cascade`, `parent_id` and `shard_count` for exactly this
   reason, and the same expression already carries a non-token guard for TTL.

4. Enforce **eagerly**: the disable call writes config and then fans out to every
   affected bucket before returning, via GSI2 (`GSI2PK={ns}/RESOURCE#{name}`,
   `GSI2SK begins_with BUCKET#`) for resource scope and GSI3
   (`GSI3PK={ns}/ENTITY#{id}`) for entity scope.

5. Raise a distinct `ResourceDisabled` exception rather than `RateLimitExceeded`.
   Disabled is closer to a 403 than a 429 and nothing should tell a client to retry.

## Scope

System-level `disabled` is **not** implemented. A system-level kill switch would have to
fan out across every bucket in the namespace (GSI4) and its blast radius warrants its own
decision. Resolution stops at the resource level.

## Consequences

**Positive:**
- Takes effect on the default fast path, which is the only path that matters in steady state.
- Limits survive the disable; re-enabling is one attribute.
- Token-bucket invariants in `models.py` and `bucket.py` are untouched.
- Callers can distinguish "intentionally off" from "temporarily saturated".

**Negative:**
- Disable is O(buckets for the resource) writes, not O(1). Bounded by entity count x shards.
- A narrow race exists between the config write and the fan-out query: an `acquire()`
  already in flight can create a bucket the fan-out's GSI query does not see. Mitigated by
  a second fan-out pass; the residual window is one in-flight acquire.
- `set_resource_defaults` / `set_limits` become read-before-write to preserve `disabled`,
  costing 1 extra RCU on an infrequent admin path.
- The provisioner's Lambda-side fan-out does not evaluate per-entity overrides; the
  handler orders resource changes before entity changes so a carve-out re-stamps last.

## Alternatives Considered

### Limit with `capacity: 0`
Rejected — see Context. Does not reach the fast path, and overloads a real bucket parameter.

### Config-only flag with no denormalization
Rejected because the fast path never reads config; the flag would only take effect on the
slow path, which steady-state traffic does not use.

### Stream-driven fan-out via the aggregator
Rejected for the initial implementation because it makes disable asynchronous with no
completion signal. A kill switch should not return before it has taken effect. Worth
revisiting as a repair mechanism for the in-flight-acquire race.

### Reserved synthetic always-failing limit (`wcu`-style)
Rejected because it still rides on `tk` and inherits the same stale-balance problem
unless tokens are explicitly zeroed.
