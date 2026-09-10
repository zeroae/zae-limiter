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

!!! note "Namespace auto-registration"
    Data-access commands internally use `Repository.open()`, which auto-registers the namespace if it doesn't exist yet. The `"default"` namespace is always registered automatically.

```bash
# Entity operations in a specific namespace
zae-limiter entity set-limits user-123 --namespace tenant-alpha -l rpm:1000

# System defaults for a namespace
zae-limiter system set-defaults --namespace tenant-alpha -l rpm:5000

# Usage and audit scoped to a namespace
zae-limiter usage list --namespace tenant-alpha
zae-limiter audit list --namespace tenant-alpha
```

## Declarative Limits

The `limits` command group manages rate limits declaratively via YAML manifest files. Changes are applied through a Lambda provisioner that tracks managed state and computes diffs, similar to how `terraform plan` and `terraform apply` work.

### YAML Manifest Format

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

### Preview Changes

```bash
# Show what would change (like terraform plan)
zae-limiter limits plan -n my-app -f limits.yaml
```

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

!!! note "Provisioner Lambda"
    The `plan`, `apply`, and `diff` subcommands invoke the `{name}-limits-provisioner` Lambda function. This function must be deployed as part of the main stack before using these commands. It is deployed by default; `zae-limiter deploy --no-provisioner` (or `--no-iam`, which leaves no role for it) skips it.

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
inherits from elsewhere in the resolution walk).

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
