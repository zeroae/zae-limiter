# Scheduled (Cron) Limits

Rate limits that change with the clock — halve throughput during business hours, raise it
overnight, close a maintenance window, or hand back a quota at the start of each period.

Without scheduling, a limit holds until someone calls `set_limits()`. The usual workaround is an
external cron job that swaps capacity at each tick, which means extra infrastructure, no
atomicity, and a window at every boundary where the old limit is still in force. Scheduled limits
move that into the library: the schedule travels with the limit, and every reader works out the
effective value for the current instant.

!!! info "Added in v0.14.0"
    `ScheduleEntry`, `Limit.with_schedule()` and `Limit.quota()` are new in v0.14.0.

## When to use it

| Pattern | Example |
|---------|---------|
| **Peak / off-peak** | Half the throughput 9–5 on weekdays, full rate overnight |
| **Maintenance window** | Drop to a trickle during a nightly batch job |
| **Quota** | 10,000 requests per calendar month, back to full on the 1st |
| **Weekend capacity** | Raise limits Saturday and Sunday when traffic is lighter |

## The mental model: a pattern, not a timer

This is the one thing worth reading twice.

In a cron *daemon*, `* 9-17 * * MON-FRI` means "fire every minute between 9 and 5 on weekdays" —
it is a list of instants. Here it means something different: **an entry is active for as long as
the current minute matches the pattern.** It describes a *window*, not a firing.

```mermaid
gantt
    title  "* 9-17 * * MON-FRI" with scale 0.5
    dateFormat HH:mm
    axisFormat %H:%M
    section Tuesday
    base limit (1000/min)   :done, a1, 00:00, 09:00
    scaled to 500/min       :crit, a2, 09:00, 18:00
    base limit (1000/min)   :done, a3, 18:00, 24:00
```

So `* 9-17 * * MON-FRI` covers every minute of the nine-hour span, and `0 9 * * MON-FRI` — which a
cron daemon would fire once a day — describes a window exactly **one minute long**. That is almost
never what you want for a limit. As a rule of thumb, leave the minute field as `*` unless you
genuinely mean a sub-hour window.

The one place a single instant *is* what you want is a quota reset — see [Quotas](#quotas).

## Scaling a limit during a window

The common case. `scale` is a multiplier on the base limit:

```{.python .lint-only}
from zae_limiter import Limit, RateLimiter, Repository, ScheduleEntry

repo = await Repository.open()
limiter = RateLimiter(repository=repo)

await limiter.set_limits(
    "user-123",
    limits=[
        Limit.per_minute("rpm", 1000).with_schedule((
            ScheduleEntry(
                cron="* 9-17 * * MON-FRI",
                tz="America/New_York",
                scale=0.5,
            ),
        )),
    ],
    resource="gpt-4",
)
```

Outside the window the limit is 1000/min. Inside it, 500/min.

!!! note "`scale` moves capacity and refill together"
    A scale of `0.5` halves **both** the bucket ceiling and the refill amount, so the time to
    refill from empty is unchanged.

## Absolute values

When a window should have its own number rather than a multiple of the base, set `capacity`
directly. `refill_amount` and `refill_period_seconds` are optional and fall back to the base:

```{.python .lint-only}
Limit.per_minute("rpm", 1000).with_schedule((
    ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),
))
```

An entry sets **either** `scale` **or** the absolute fields — never both. Mixing them raises
`ValueError` at construction.

## Several windows: first match wins

Entries are checked in order and the first one matching the current minute supplies the limit.
Nothing merges, and nothing accumulates:

```{.python .lint-only}
Limit.per_minute("rpm", 1000).with_schedule((
    # Weekends are quiet — most specific first.
    ScheduleEntry(cron="* * * * SAT,SUN", tz="America/New_York", scale=2.0),
    # Business hours on the remaining days.
    ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
    # Everything else falls through to the base limit.
))
```

If no entry matches, the base limit applies. Order the specific before the general.

## Timezones

Every entry carries its own IANA timezone, defaulting to `UTC`:

```{.python .lint-only}
ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="Europe/Berlin", scale=0.5)
```

!!! warning "The default is UTC, not your local time"
    `tz` defaults to `"UTC"`. "Business hours" almost never means business hours in UTC — set it
    explicitly unless you really mean UTC.

Daylight saving is handled for you. A `9-17 America/New_York` window stays 9 a.m. to 5 p.m. local
on both sides of a transition; only the corresponding UTC instant shifts. You do not need to
adjust anything twice a year, and the 23-hour and 25-hour days are counted correctly.

!!! warning "One timezone per limit, and per configuration level"
    Every entry on one limit must name the same `tz`, and so must every scheduled limit written
    to the same configuration level — one `set_limits()`, one `set_resource_defaults()`, one
    `limits:` block in a manifest. Through the Python API, mixing zones raises `ValueError`
    before anything is written.

    So an entity whose `rpm` follows New York and whose `tpm` follows Berlin needs them on
    separate resources. Limits without a schedule can sit alongside any zone.

## Quotas

A token bucket drips: tokens come back gradually, at the refill rate. That is right for a rate
limit and wrong for an allowance. A **quota** hands back its whole balance at a calendar instant
and does not recover in between — spend it, and you wait for the reset.

`Limit.quota()` builds one. The period is whatever the cron expression says:

```{.python .lint-only}
from zae_limiter import Limit

await limiter.set_limits(
    "user-123",
    limits=[
        # 10,000 requests per calendar month, back to full at midnight on the 1st.
        Limit.quota("monthly", 10_000, cron="0 0 1 * *", tz="America/New_York"),
    ],
    resource="gpt-4",
)
```

Any other period is the same call with a different expression:

```{.python .lint-only}
Limit.quota("session", 500, cron="0 */5 * * *", tz="America/New_York")    # every five hours
Limit.quota("weekly", 50_000, cron="0 0 * * MON", tz="America/New_York")  # Monday midnight
Limit.quota("daily", 10_000, cron="0 0 * * *", tz="America/New_York")     # local midnight
```

A quota has no refill rate: a limit either drips or resets, never both
([ADR-137](../adr/137-reset-replaces-drip.md)). Pairing a positive `refill_amount` with a
`reset_schedule` raises `ValueError` at construction, because the drip running underneath the
reset hands back roughly twice the intended allowance each period. A zero rate with no reset is
rejected too — that bucket could never recover.

A reset fires on the **edge**, not across a window: it applies on the transition *into* matching,
so `0 0 * * *` is right here even though the same expression would be a one-minute window as a
`schedule` entry.

!!! note "Idle buckets reset when they wake"
    A reset applies on the first request after its edge, not at the edge itself. A bucket idle
    from 18:00 to 09:00 the next morning gets its reset on that 09:00 request. Several missed
    edges apply once — setting the balance to the allowance is idempotent.

!!! tip "Usage history is unaffected"
    A reset restores tokens without touching the total-consumed counter, so
    [usage snapshots](usage-snapshots.md) stay continuous across it. Resets do not create gaps or
    resets in your usage reporting.

## Precedence: override, not merge

Schedules travel with the limit they belong to, through the normal
[configuration hierarchy](config-hierarchy.md). The level that wins supplies **everything** —
numbers and schedule together.

That has one consequence people trip over: an entity-level `rpm` with **no** schedule *removes*
the resource-level schedule for that entity, exactly as it already replaces the numbers.

```{.python .lint-only}
# Resource level: everyone gets the business-hours reduction.
await limiter.set_resource_defaults("gpt-4", limits=[
    Limit.per_minute("rpm", 1000).with_schedule((
        ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
    )),
])

# This entity is now UNSCHEDULED at 2000/min — the resource schedule does not carry over.
await limiter.set_limits("enterprise-1", limits=[Limit.per_minute("rpm", 2000)],
                         resource="gpt-4")

# To keep a schedule for this entity, state it.
await limiter.set_limits("enterprise-1", resource="gpt-4", limits=[
    Limit.per_minute("rpm", 2000).with_schedule((
        ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
    )),
])
```

## Declarative configuration

Schedules round-trip through [YAML manifests](../infra/deployment.md) and the
`Custom::ZaeLimiterLimits` CloudFormation resource:

```yaml
namespace: default
resources:
  gpt-4:
    limits:
      rpm:
        capacity: 1000
        schedule:
          - cron: "* 9-17 * * MON-FRI"
            tz: America/New_York
            scale: 0.5
          - cron: "* 0-6 * * *"
            tz: America/New_York
            capacity: 2000
      rpd:
        capacity: 10000
        reset_schedule:
          - cron: "0 0 * * *"
            tz: America/New_York
```

```bash
zae-limiter limits plan -n my-app -f limits.yaml    # preview
zae-limiter limits apply -n my-app -f limits.yaml
```

A `reset_schedule` makes the limit a quota, so `refill_amount` defaults to `0` and you do not
write it. Giving a quota a non-zero `refill_amount` is an error.

Invalid cron expressions, unknown timezones and a rate beside a reset are rejected at **parse**
time, so `limits plan` catches them before anything is written.

## Viewing a schedule

The CLI shows schedules but does not set them — use the Python API or a manifest:

```console
$ zae-limiter entity get-limits user-123 --resource gpt-4
Limits for user-123 (gpt-4):
  rpm: 1,000/min
    Schedule:
      "* 9-17 * * MON-FRI" America/New_York  → scale 50%
      "* 0-6 * * *" America/New_York  → capacity 2,000
  rpd: 10,000 quota (resets "0 0 * * *" America/New_York)
```

!!! note "Weekdays and months display as names"
    A schedule written as `1-5` comes back as `MON-FRI`, and `1,7` as `JAN,JUL`. The meaning is
    identical — the stored form is canonical and renders with names for readability.

!!! note "A quota renders as an allowance, not a rate"
    A quota has no refill rate, so its line names the whole allowance and the cron that hands it
    back. The indented `Schedule:` block is for windows that override parameters; a reset
    overrides nothing, so it stays on the headline.

## What happens at a boundary

Normal requests are unaffected by scheduling: the cost and latency of an `acquire()` inside a
window are exactly what they are without one.

At the moment a window opens or closes, the **first** request to arrive takes the slow path while
the bucket is brought up to date: three round trips instead of one, and roughly twice the
DynamoDB cost for that one request. Requests already in flight at that instant pay it too. After
that the cost returns to normal until the next boundary. For a typical peak/off-peak schedule
that is two such requests per bucket per day.

Idle buckets do nothing at a boundary, correctly — they update on their next request.

`RateLimitExceeded.retry_after_seconds` walks boundaries rather than assuming the rate in force
right now holds forever: if the limit rises in ten minutes, the wait reflects that. For a quota,
which has no rate to divide by, the wait is the time to the next reset edge — exhaust a
`0 0 * * *` quota at 18:00 in New York and it reports six hours.

## Limitations

- **A quota period is a fixed calendar window, shared by everyone on it.** The cron expression
  names wall-clock instants, so `0 */5 * * *` resets at 00:00, 05:00, 10:00 … for **every**
  entity alike — not five hours after each caller's own first request. A window anchored to each
  caller's own activity is not supported; it may arrive in a later release
  ([ADR-138](../adr/138-fixed-reset-windows-only.md)). Note also that resetting every entity at
  the same instant concentrates load at the boundary.
- **One time-varying mechanism per bucket.** A bucket uses cron scheduling or another dynamic
  mechanism, not both. This keeps "why is my limit this number" answerable.
- **Extended cron syntax is not supported.** `L` (last), `W` (weekday) and `#` (nth weekday) are
  rejected at construction. Standard ranges, lists, steps and names — `1-5`, `1,3`, `*/15`,
  `MON-FRI`, `JAN,JUL` — all work.
- **Sunday is `0` inside a range or a step.** On its own or in a list, Sunday may be written
  `0`, `7` or `SUN` and all three store identically. In a range or a step it must be `0` —
  `SUN-THU` is `0-4`, `SUN/2` is `0/2` — and a range that *ends* at Sunday (`MON-SUN`,
  `SAT-SUN`, `7-4`) runs backwards and is rejected; write `*` or a list instead.
- **Seconds are not addressable.** The finest granularity is one minute, and an expression must
  have exactly five fields. The six-field form some schedulers accept, with a leading *seconds*
  field, raises `ValueError` at construction.
- **Resource- and system-level schedule changes reach existing buckets when those buckets expire**
  rather than immediately, consistent with how default-derived limits already behave. Entity-level
  changes take effect immediately.

## See also

- [Configuration Hierarchy](config-hierarchy.md) — how schedules resolve across levels
- [Token Bucket Algorithm](token-bucket.md) — why a quota reset is not the same as a refill
- [Basic Usage](basic-usage.md) — `acquire()`, leases, and handling `RateLimitExceeded`
