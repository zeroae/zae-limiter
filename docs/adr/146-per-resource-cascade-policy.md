# ADR-146: Per-resource cascade policy, resolved like `disabled`

**Status:** Accepted
**Date:** 2026-10-05
**Issue:** [#676](https://github.com/zeroae/zae-limiter/issues/676)
**Supersedes:** [ADR-112](112-cascade-per-entity.md) in part (where the cascade decision comes from)
**Related:** [ADR-112](112-cascade-per-entity.md), [ADR-125](125-resource-disable.md), [ADR-141](141-reset-after-version-gate.md), [#674](https://github.com/zeroae/zae-limiter/issues/674), [#675](https://github.com/zeroae/zae-limiter/issues/675), [#677](https://github.com/zeroae/zae-limiter/issues/677), [#684](https://github.com/zeroae/zae-limiter/issues/684), [#686](https://github.com/zeroae/zae-limiter/issues/686)

## Context

`cascade` is set once per entity at `create_entity()`, stored on the entity's META record,
copied onto every bucket item the entity owns, and cached per entity in
`Repository._entity_cache` as `(cascade, parent_id, shard_counts)`. It applies to **every**
resource the entity acquires and cannot be changed.

The LLM gateway use case (#674) needs it to differ by resource for the same entity:

| Resource | Cascade | Why |
|----------|---------|-----|
| One per model (`gpt-4`, `claude-sonnet`, …) | **on** | The org's per-model limit protects the provider's quota |
| `llm` (the shared cost budget) | **off** | The budget is per user; cascading it costs a write per call for nothing |

The same shape is already solved for `disabled` (ADR-125): a tri-state value on config
items, resolved by an independent walk, denormalised onto bucket items so the fast path
decides with no config read, and stamped eagerly by a fan-out when it changes.

Three facts about the current code shape the decision:

- `resolve_disabled()` already reads, in full, the three config items the walk needs
  (entity(resource), entity(`_default_`), resource), on every slow path. A second value
  on those items costs no extra read.
- Every bucket item already carries `cascade` and `parent_id`, and since #684 the slow
  path's rf-locked write re-stamps both from the owner's META on every pass.
- The aggregator never reads `cascade`; its Path 2 clone copies the item verbatim.

## Decision

`cascade` must be decided per (entity, resource): a tri-state policy on resource and entity
config items (absent inherits; not on system config) resolved by the ADR-125 walk, falling
back to the entity's META `cascade` when no level sets one, and never cascading for an entity
without a parent. The effective policy must be stamped on every bucket item by every
slow-path write and by an eager fan-out on each change (resolving each bucket's own entity
and resource from strongly consistent reads), and the fast path must decide from that stamp
with no config read. Writing a policy must pass the ADR-141 version gate at 0.16.0 and raise
`client_min_version` to at least 0.16.0, capped at a development writer's own build and never
lowering it. The repository methods it adds are required members of `RepositoryProtocol`, not
a capability-gated option (ADR-109).

**Partially supersedes ADR-112.** Whether the parent is included is decided per
(entity, resource) by the resolved policy; ADR-112's META `cascade` remains the default when
no level sets one. ADR-112's other decisions stand: no per-call parameter, the child decides,
and `cascade` is aliased as a reserved word.

The decisions in detail — cache, self-healing stamps, fan-out, gate, surfaces, the
`limits plan` warning, #677 — and the cost table are in
`docs/plans/2026-10-05-cascade-policy-design.md`. They were agreed with the owner on
2026-10-04 during the v0.16.0 planning for #674.

## Consequences

**Positive**
- Cascade can differ per resource for one entity, with no new mechanism: the same
  config shape, walk, stamp and fan-out as `disabled`.
- Fully backward compatible when no policy is set.
- No cost on the fast or slow path.

**Negative**
- A policy change is O(buckets), not O(1), like disabling.
- The ADR-125 race remains: an acquire that read config before a policy write can create
  a bucket the fan-out misses. It is corrected on that bucket's next slow pass, but a
  bucket that only takes the fast path keeps the old policy until then.
- A v0.14 client ignores `client_min_version` and would decide cascade from META. A
  pre-0.16 admin's full-replace config write drops the policy (same accepted risk as
  ADR-142's hidden config).
- A cache entry per (entity, resource) is larger than one per entity.

## Alternatives considered

- **Make the entity flag mutable (`set_cascade(entity_id, bool)`)**: one value per entity
  cannot express "models cascade, budget does not", which is the whole use case.
- **Cascade as a property of the resource only**: cannot exempt or include one entity;
  the walk costs nothing extra, so there is no reason to give up per-entity control.
- **Two `acquire()` calls (one cascading, one not)**: two leases, two round trips and
  two reconciles per request; rejected by the owner during #674 planning.
- **Read the policy on the fast path**: adds a config read to every call; the item
  stamp already exists for this purpose.
- **Lazy, self-healing only (no fan-out)**: a bucket that only takes the fast path
  would never pick up the change.
