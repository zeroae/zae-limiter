# RateLimiter

The main rate limiter classes for async and sync usage.

## RateLimiter (Async)

::: zae_limiter.limiter.RateLimiter
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## SyncRateLimiter

::: zae_limiter.sync_limiter.SyncRateLimiter
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## OnUnavailable

::: zae_limiter.limiter.OnUnavailable
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

## Lease (Async)

The object yielded by `RateLimiter.acquire()`. Only limits declared in
`acquire(consume=...)` are adjustable through it; see
[Adjusting Consumption](../guide/basic-usage.md#adjusting-consumption).
A lease from `acquire(..., also={...})` covers several resources: its own
methods act on the primary resource and `resource(name)` reaches the others; see
[Several Resources in One Acquire](../guide/multi-resource.md).

::: zae_limiter.lease.Lease
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
      members:
        - degraded
        - resources
        - resource
        - consumed
        - adjust
        - consume
        - release

## SyncLease

::: zae_limiter.sync_lease.SyncLease
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
      members:
        - degraded
        - consumed
        - adjust
        - consume
        - release
        - resources
        - resource

## LeaseResource (Async)

One resource of a multi-resource lease, returned by `Lease.resource(name)` (ADR-148).

::: zae_limiter.lease.LeaseResource
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## SyncLeaseResource

::: zae_limiter.sync_lease.SyncLeaseResource
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
