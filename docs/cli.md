# CLI Reference

## Commands

::: mkdocs-click
    :module: zae_limiter.cli
    :command: cli
    :prog_name: zae-limiter
    :depth: 2
    :style: table
    :list_subcommands: true

## Environment Variables

The CLI respects standard AWS environment variables:

| Variable | Description |
|----------|-------------|
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_SESSION_TOKEN` | AWS session token |
| `AWS_DEFAULT_REGION` | Default AWS region |
| `AWS_PROFILE` | AWS profile name |
| `AWS_ENDPOINT_URL` | Custom endpoint URL |

## Exit Codes

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | General error |
| `2` | Invalid arguments |
| `3` | AWS API error |
| `4` | Stack not found |

## Namespace Flag

Most data-access commands accept `--namespace` / `-N` to scope operations to a specific namespace. When omitted, operations default to the `"default"` namespace.

!!! note "Namespace registration"
    Commands that **write** (`set-*`, `delete-*`, `entity create`, `disable` / `enable` / `clear-disabled`, `set-cascade` / `clear-cascade`, `limits apply`) register the namespace if it does not exist yet. Commands that only **read** never do: they exit 1 and name the register command (see [Read-Only Commands](#read-only-commands)).

```bash
# Entity operations in a specific namespace
zae-limiter entity set-limits user-123 --namespace tenant-alpha -l rpm:1000

# System defaults for a namespace
zae-limiter system set-defaults --namespace tenant-alpha -l rpm:5000

# Usage and audit scoped to a namespace
zae-limiter usage list --namespace tenant-alpha
zae-limiter audit list --namespace tenant-alpha
```

## Read-Only Commands

Commands that only read never change anything: they never deploy a stack, register a
namespace, write the version record, or push Lambda code. When something they need is missing,
they stop instead of creating it.

| Command | Reads |
|---------|-------|
| `status`, `check`, `version`, `list` | Stack, table and version record |
| `entity show`, `entity get-limits`, `entity list`, `entity list-resources` | One namespace |
| `resource get-defaults`, `resource list`, `system get-defaults` | One namespace |
| `audit list`, `usage list`, `usage summary` | One namespace |
| `namespace list`, `namespace show`, `namespace orphans` | The namespace registry |
| `limits plan`, `limits diff` | One namespace, through the provisioner Lambda |

`status`, `check` and `version` report a missing stack or an out-of-date Lambda as part of their
output. Every other read-only command stops with exit code 1:

| Situation | Message |
|-----------|---------|
| Stack missing | `Error: Stack '<name>' not found in <region>. Deploy it with 'zae-limiter deploy -n <name>'.` |
| Namespace missing | `Error: Namespace '<ns>' not found. Register it with 'zae-limiter namespace register <ns>'.` |
| Client below the stack's minimum version | `Error: Version mismatch: ... Please upgrade.` |

**Lambdas behind the client.** A plain read does not use the Lambdas, so it runs normally and
leaves them alone. `limits plan` and `limits diff` hand the manifest to the provisioner Lambda,
so they **require an up-to-date stack** and refuse otherwise, before invoking it:

```
Error: the stack's Lambdas run 0.14.0; this client is 0.15.0. Run 'zae-limiter upgrade -n my-app' first, then re-run the plan.
```

A stack whose Lambda version is unknown (no version record, or one written by a client that
deployed no Lambda code) is refused the same way, as `run unknown`.

Every other command intends to write and keeps its behaviour: `deploy` and `upgrade` provision
and update, and the data commands that write register a missing namespace.

## Declarative Limits

The `limits` command group manages rate limits declaratively via YAML manifest files. Changes are applied through a Lambda provisioner that tracks managed state and computes diffs, similar to how `terraform plan` and `terraform apply` work.

### YAML Manifest Format

Each level's `limits` replaces the levels below it. To reuse limits across levels without
repeating them, see [Reusing Limits with YAML Anchors](infra/deployment.md#reusing-limits-with-yaml-anchors).

```yaml
namespace: default

system:
  on_unavailable: block
  limits:
    rpm:
      capacity: 1000
    tpm:
      capacity: 100000

resources:
  gpt-4:
    limits:
      rpm:
        capacity: 500
      tpm:
        capacity: 50000
  gpt-3.5-turbo:
    limits:
      rpm:
        capacity: 2000
      tpm:
        capacity: 500000

entities:
  user-premium:
    resources:
      gpt-4:
        limits:
          rpm:
            capacity: 1000
          tpm:
            capacity: 100000
```

Only `capacity` is required per limit. Defaults: `refill_amount` = `capacity`, `refill_period` = `60` seconds.

A limit may also carry a `schedule`, which changes its parameters while the current minute
matches a cron pattern, and a `reset_schedule`, which restores the balance to the full
allowance when a window opens:

```yaml
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

Both take standard 5-field cron. A `schedule` entry sets either `scale` or the absolute fields
(`capacity`, `refill_amount`, `refill_period_seconds`); entries are checked in order and the
first match wins. A `reset_schedule` entry takes `cron` and `tz` only.

A limit with a `reset_schedule` does not drip: `refill_amount` defaults to `0` rather than to
`capacity`, and setting it to anything positive is rejected. A limit drips or resets, never
both. `Schedule` and `ResetSchedule` round trip through the generated
`Custom::ZaeLimiterLimits` resource.

A quota can instead reset a fixed time after each entity's **own** first use — a
[session quota](guide/session-quotas.md). Give `reset_after_seconds` (a whole number of seconds)
in place of `reset_schedule`; the two are mutually exclusive, and `refill_amount` again defaults
to `0`:

```yaml
limits:
  session:
    capacity: 10000
    reset_after_seconds: 18000   # 5h from each entity's own first use
```

`reset_after_seconds` round trips through `Custom::ZaeLimiterLimits` as `ResetAfterSeconds`.

### Soft Limits

Any `limits.<name>` mapping, at every level, may set `soft: true`: the limit is metered and
debited but never rejects a request ([Soft Limits and Bypass](guide/soft-limits-and-bypass.md)).
Omitting it makes the limit hard — the manifest owns it like every other field of the limit.

```yaml
resources:
  gpt-4:
    limits:
      rpm: {capacity: 500}
      tpm: {capacity: 50000, soft: true}
```

`soft` must be a boolean. It round trips through `Custom::ZaeLimiterLimits` as a `Soft`
property on each limit. A resource- or system-level change of soft-ness restamps the existing
buckets of that level on `apply`; a routine apply that changes none writes no buckets for it.

### Cascade and Disabled

Entries under `resources.<name>` and `entities.<id>.resources.<name>` may also set two flags,
each `true`, `false`, or omitted; `disabled` also takes `bypass`:

| Field | Meaning | See |
|-------|---------|-----|
| `disabled` | Turn the resource off (or re-admit one entity); `bypass` admits without debiting | [Disabling Resources and Entities](#disabling-resources-and-entities), [Bypass](#bypass) |
| `cascade` | Whether acquires on this resource also debit the parent | [Cascade Policy per Resource](#cascade-policy-per-resource) |

```yaml
resources:
  gpt-4:
    cascade: true
    limits:
      tpm: {capacity: 10000}
  llm:
    cascade: false
    limits:
      cost: {capacity: 500}
entities:
  org-acme:
    resources:
      gpt-4:
        limits:
          tpm: {capacity: 100000}
```

- **The manifest owns both flags** for every item it declares. Omitting one clears a value set
  earlier by `set-cascade`, `disable` or the Python API on the next `apply`.
- **Neither applies to `system`.** Either one there is an error, so `plan`, `apply` and `diff`
  fail before anything is written. Set them on `resources.<name>` or
  `entities.<id>.resources.<name>` instead.
- **`cascade` must be a boolean**; a value like `"yes"` fails the plan.
- **Bucket restamps:** `disabled` restamps every declared resource and entity level on each apply. `cascade`
  restamps only the levels whose stored policy actually changed, so a routine apply writes no
  buckets for it.
- **CloudFormation:** both round trip through `Custom::ZaeLimiterLimits` as the `Disabled` and
  `Cascade` properties on `Resources` and `Entities` entries. Either one under `System` fails
  the stack operation.
- **Version:** a manifest that sets `cascade` needs a stack whose Lambdas are 0.16.0 or later,
  and raises the stack's minimum client version to 0.16.0. One that sets `soft: true` or
  `disabled: bypass` needs 0.17.0 or later and raises the minimum to 0.17.0.
- **`disabled` must be `true`, `false` or `bypass`**; CloudFormation also accepts `"Bypass"`.

`limits plan` warns when a resource sets `cascade: true` and no entity in the manifest has its
own limits for it, because parents would then be limited by the per-user resource defaults:

```
Warning: resources.gpt-4 sets cascade: true, but no entity in this manifest has its own limits for 'gpt-4', so parents will be limited by the per-user resource defaults
```

### Preview Changes

```bash
# Show what would change (like terraform plan)
zae-limiter limits plan -n my-app -f limits.yaml
```

`plan` is read-only: it never deploys the stack, registers the manifest's namespace, or updates
the Lambdas. It needs a deployed stack, a registered namespace and **up-to-date Lambdas**; if
the Lambdas are behind this client, run `zae-limiter upgrade -n my-app` first (see
[Read-Only Commands](#read-only-commands)). A manifest for a new namespace can be applied
directly — `limits apply` registers it.

Output:
```
Plan: 4 change(s)

  + create system: (system defaults)
  + create resource: gpt-4
  + create resource: gpt-3.5-turbo
  + create entity: user-premium/gpt-4
```

### Apply Changes

```bash
# Apply limits from YAML file
zae-limiter limits apply -n my-app -f limits.yaml
```

Output:
```
  + create system: (system defaults)
  + create resource: gpt-4
  + create resource: gpt-3.5-turbo
  + create entity: user-premium/gpt-4

Applied: 4 created, 0 updated, 0 deleted.
```

Subsequent applies with a modified YAML file will show `~` for updates and `-` for deletions (items removed from the manifest are deleted from DynamoDB).

### Detect Drift

```bash
# Show drift between YAML and live DynamoDB state
zae-limiter limits diff -n my-app -f limits.yaml
```

`diff` is read-only with the same requirements as `plan`.

Output (when live state differs from YAML):
```
Drift detected: 1 difference(s)

  ~ resource: gpt-4
```

### Generate CloudFormation Template

```bash
# Generate a CFN template with Custom::ZaeLimiterLimits resource
zae-limiter limits cfn-template -n my-app -f limits.yaml > limits-stack.yaml

# Deploy with AWS CLI
aws cloudformation deploy \
    --template-file limits-stack.yaml \
    --stack-name my-app-limits
```

The generated template uses `Custom::ZaeLimiterLimits` backed by the provisioner Lambda. The Lambda ARN is imported from the main stack via `Fn::ImportValue`.

### Common Options

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Stack identifier (required) |
| `--file` | `-f` | Path to YAML limits file (required) |
| `--region` | | AWS region |
| `--endpoint-url` | | Custom endpoint URL (e.g., LocalStack) |
| `--namespace` | `-N` | Namespace (default: `"default"`) |

### Partial Failures

Config writes are committed before the fan-out that carries them to live bucket items, so an
apply can get part-way and stop. `limits apply` reports what landed, prints each failure on
stderr, and exits 1:

```
Applied: 2 created, 1 updated, 0 deleted.

Errors (1):
  - bucket param sync entity user-123: <reason>
```

Every write is idempotent, so re-running the same manifest reconciles the remainder. The
`#PROVISIONER` record is written either way, so it always describes the config that is actually
in the table.

!!! note "Provisioner Lambda"
    The `plan`, `apply`, and `diff` subcommands invoke the `{name}-limits-provisioner` Lambda function. This function must be deployed as part of the main stack before using these commands. It is deployed by default; `zae-limiter deploy --no-provisioner` (or `--no-iam`, which leaves no role for it) skips it.

## Schedules and Quotas

`system get-defaults`, `resource get-defaults` and `entity get-limits` render a limit's
schedule beneath it. Cron is shown canonically, with weekday and month as names, followed by
the timezone and what the window does:

```
Limits for entity 'user-123' on resource 'gpt-4':
  rpm: 1,000/min
    Schedule:
      "* 9-17 * * MON-FRI" America/New_York  → scale 50%
      "* 0-6 * * *" America/New_York  → capacity 2,000
```

A quota carries its whole allowance and the instant it comes back on one line:

```
Limits for entity 'user-123' on resource 'gpt-4':
  session: 500 quota (resets "0 */5 * * *" America/New_York)
  rpmo: 1,000,000 quota (resets "0 0 1 * *" America/New_York)
```

A [session quota](guide/session-quotas.md) names its window length and says "after first use",
so it is not mistaken for a clock-aligned period:

```
Limits for entity 'user-123' on resource 'claude-sonnet':
  session: 10,000 quota (resets 5h after first use)
```

Schedules and session windows are set through the Python API or a YAML manifest. The
`-l name:rate/period` flag on `set-defaults` and `set-limits` takes neither a cron expression
nor a `reset_after` window.

!!! warning "`-l` replaces the whole level"
    A set writes the level's limits in full, and a limit built from `-l` carries no schedule
    and no session window. Running `entity set-limits user-123 -r gpt-4 -l rpm:1000` against a
    level whose stored `rpm` is scheduled therefore drops that schedule, and a stored quota —
    calendar or session — becomes a dripping limit. Edit such levels through `limits apply` or
    the Python API.

## Disabling Resources and Entities

The `resource` and `entity` command groups include `disable`, `enable`, and `clear-disabled`
subcommands for turning access off without deleting stored limits. See
[ADR-125](adr/125-resource-disable.md) for the full design.

Disabling is eager: existing buckets are stamped immediately, so the change takes effect on
the very next request — no cache TTL or refill delay to wait out. `acquire()` raises a
distinct `ResourceDisabled` exception (not `RateLimitExceeded`) for a disabled resource or
entity; map it to HTTP 403, not 429, since retrying will not help.

```bash
# Turn off a resource for everyone without an entity-level override
zae-limiter resource disable gpt-4

# Explicitly enable a resource that is disabled by default
zae-limiter resource enable gpt-4

# Revert a resource to inheriting the disabled state
zae-limiter resource clear-disabled gpt-4
```

```bash
# Disable an entity across all resources
zae-limiter entity disable user-123

# Disable an entity for a specific resource only
zae-limiter entity disable user-123 --resource gpt-4

# Re-admit an entity to a resource that is disabled for everyone else
zae-limiter entity enable user-123 --resource gpt-4

# Revert an entity to inheriting the resource's disabled state
zae-limiter entity clear-disabled user-123 --resource gpt-4
```

An entity-level `disabled: false` override always wins, even when the resource is disabled
for everyone else — resolution walks entity (resource-specific) → entity (`_default_`) →
resource, and the first level with an explicit value wins regardless of which level supplies
the limits. There is no system-level disable.

`resource get-defaults` and `entity get-limits` report that level's own explicit `disabled`
value when it has one — not the fully resolved state across the entity → resource walk. A
resource showing no `Status:` line can still be effectively disabled for a given entity because
some other level in the walk sets it; use `resolve_disabled()` (see
[Resource or Entity Disabled](operations/rate-limits.md#resource-or-entity-disabled)) to get the
actual resolved outcome for a specific entity+resource pair.

```
Defaults for resource 'gpt-4':
  rpm: 500/min
Status: DISABLED
```

```
Limits for entity 'user-123' on resource 'gpt-4':
  rpm: 1000/min
Status: enabled (explicit override)
```

No `Status:` line is printed when the level has no explicit `disabled` value (i.e. it
inherits from elsewhere in the resolution walk). A level set to bypass prints
`Status: BYPASSED` (see [Bypass](#bypass)).

## Soft Limits

`system set-defaults`, `resource set-defaults` and `entity set-limits` take a repeatable
`--soft NAME`, which makes the named `-l` limit soft: metered and debited, but never a reason to
reject. See [Soft Limits and Bypass](guide/soft-limits-and-bypass.md).

```bash
# Hard rpm, soft (metered-only) tpm
zae-limiter resource set-defaults gpt-4 -l rpm:500 -l tpm:50000 --soft tpm
```

`--soft` must name a limit given with `-l`. A set is a full replace, so a `set-*` without
`--soft tpm` turns a soft `tpm` hard again. `get-defaults` and `get-limits` mark a soft limit
`(soft)`:

```
Defaults for resource 'gpt-4':
  rpm: 500/min
  tpm: 50,000/min (soft)
```

## Bypass

`resource bypass` and `entity bypass` admit every request without debiting any limit, while
still counting consumption; only the reserved `wcu` write limit still gates. Bypass is the third
value of `disabled`, so it resolves by the same walk and is lifted with `clear-disabled`.

```bash
zae-limiter resource bypass gpt-4
zae-limiter entity bypass vip-1                   # every resource
zae-limiter entity bypass vip-1 --resource gpt-4  # one resource

zae-limiter resource clear-disabled gpt-4         # lift it
```

`get-defaults` and `get-limits` print `Status: BYPASSED` for a level that sets it. Storing a soft
limit or a bypass needs a stack whose Lambdas are 0.17.0 or later and raises the stack's minimum
client version to 0.17.0.

## Cascade Policy per Resource

`set-cascade` and `clear-cascade` on the `resource` and `entity` groups decide, per resource,
whether an entity's acquires also debit its parent. Use it when one entity needs different
answers on different resources — for example, model limits that count against the org while a
shared budget stays per user. See [ADR-146](adr/146-per-resource-cascade-policy.md).

```bash
# Model limits cascade to the parent; the shared budget does not
zae-limiter resource set-cascade gpt-4 on
zae-limiter resource set-cascade llm off

# One entity: on for one resource, or off for every resource without its own policy
zae-limiter entity set-cascade user-123 on --resource gpt-4
zae-limiter entity set-cascade user-123 off

# Revert to inheriting
zae-limiter resource clear-cascade llm
zae-limiter entity clear-cascade user-123 --resource gpt-4
```

The policy resolves like `disabled`: entity (resource-specific) → entity (`_default_`) →
resource, first explicit value wins. When no level sets it, the entity's own `cascade` flag
from `entity create --cascade` applies, so stacks that never set a policy behave as before. An
entity with no parent never cascades.

Like disabling, a change is eager: existing buckets are restamped immediately. Setting or
clearing a policy needs a stack whose Lambdas are 0.16.0 or later — otherwise the command exits
1 and names `zae-limiter upgrade` (or `zae-limiter deploy`, when the stack has no version record)
— and raises the stack's minimum client version to 0.16.0.

`resource get-defaults` and `entity get-limits` print `Cascade: on (explicit)` or
`Cascade: off (explicit)` when that level sets a policy, and nothing when it inherits.

## Namespace Lifecycle

The `namespace` command group manages the namespace registry:

```bash
# Register namespaces
zae-limiter namespace register tenant-alpha tenant-beta

# List active namespaces
zae-limiter namespace list

# Show namespace details (including opaque ID)
zae-limiter namespace show tenant-alpha

# Soft delete (data preserved, forward lookup removed)
zae-limiter namespace delete tenant-alpha

# Recover a soft-deleted namespace
zae-limiter namespace recover <namespace-id>

# List deleted namespaces (candidates for purge)
zae-limiter namespace orphans

# Hard delete all data in a namespace (irreversible)
zae-limiter namespace purge <namespace-id> --yes
```
