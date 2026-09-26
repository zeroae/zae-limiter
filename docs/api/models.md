# Models

Data models for rate limit configuration and status.

## Limit

::: zae_limiter.models.Limit
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

### Session windows: `Limit.reset_after`

`reset_after: timedelta | None` is the second spelling of a quota's reset
([ADR-139](../adr/139-duration-reset-windows.md)): the allowance returns this long after the
entity's **own** first admitted use, and the next admitted request after that opens a fresh
window. Build one with `Limit.quota(name, capacity, reset_after=timedelta(...))`; `quota()` takes
exactly one of `cron` or `reset_after`. It must be a positive whole number of seconds, at most
10⁹. `Limit.reset_after_seconds` is the same value as an `int`, the spelling used by `to_dict()`,
YAML manifests (`reset_after_seconds`) and CloudFormation (`ResetAfterSeconds`). See
[Session Quotas](../guide/session-quotas.md).

## ScheduleEntry

One window of a limit's `schedule`, or one edge of its `reset_schedule`. Build reset entries
with `ScheduleEntry.reset()`, which takes `cron` and `tz` only.

Everything is validated at construction, so an entry that applies is an entry that evaluates.
`ValueError` is raised for a cron expression that will not parse, a `tz` that is not a
resolvable IANA name, an entry setting neither `scale` nor an absolute field or setting both,
and any modifier that is not positive and finite. The absolute fields — `capacity`,
`refill_amount`, `refill_period_seconds` — are integers, so a fractional value such as `100.5`
is rejected rather than rounded. `scale` is the field that takes a fraction.

::: zae_limiter.schedule.ScheduleEntry
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## Entity

::: zae_limiter.models.Entity
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## LimitStatus

::: zae_limiter.models.LimitStatus
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

`resets_at_ms: int | None` is the absolute epoch-millisecond instant a **session quota's**
current window ends, read off the bucket item. It is `None` for a rate limit, for a calendar
quota (whose next edge `RateLimitExceeded.as_dict()` computes from the cron instead), and for a
session quota with no live window — it is never an instant in the past. Inside
`RateLimitExceeded` it describes the shard the request was tried on; inside
`Availability` it is the latest live window end across the entity's shards.

## Availability

::: zae_limiter.models.Availability
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## BucketState

::: zae_limiter.models.BucketState
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## AuditEvent

::: zae_limiter.models.AuditEvent
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## AuditAction

::: zae_limiter.models.AuditAction
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## UsageSnapshot

::: zae_limiter.models.UsageSnapshot
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## UsageSummary

::: zae_limiter.models.UsageSummary
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## LimiterInfo

::: zae_limiter.models.LimiterInfo
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## StackOptions

::: zae_limiter.models.StackOptions
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## Status

::: zae_limiter.models.Status
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## BackendCapabilities

::: zae_limiter.models.BackendCapabilities
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## ResourceCapacity

::: zae_limiter.models.ResourceCapacity
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## EntityCapacity

::: zae_limiter.models.EntityCapacity
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## SpeculativeResult

::: zae_limiter.repository_protocol.SpeculativeResult
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## CacheStats

::: zae_limiter.config_cache.CacheStats
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
