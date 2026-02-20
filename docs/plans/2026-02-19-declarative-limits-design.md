# Declarative Limits Management Design

**Issue:** [#405](https://github.com/zeroae/zae-limiter/issues/405)
**Date:** 2026-02-19

## Overview

Operators declare rate limits in YAML files (one per namespace) and apply them through a Lambda provisioner that serves both CLI and CloudFormation paths. Terraform-style state tracking ensures unmanaged limits are left alone.

## Architecture

```
YAML --> CLI parse --> Lambda invoke --> diff vs #PROVISIONER --> Repository API --> DynamoDB
CFN event ----------> Lambda ---------> diff vs #PROVISIONER --> Repository API --> DynamoDB
```

- The Lambda is the only writer (no direct DynamoDB access from CLI)
- LocalStack supports Lambda containers, so no fallback is needed
- One YAML file per namespace, one CFN stack per namespace

## YAML Schema

```yaml
# tenant-alpha.limits.yaml
namespace: tenant-alpha

system:
  on_unavailable: allow
  limits:
    rpm:
      capacity: 10000
    tpm:
      capacity: 100000

resources:
  gpt-4:
    limits:
      rpm:
        capacity: 1000
      tpm:
        capacity: 50000
        burst: 75000
        refill_amount: 50000
        refill_period: 60
  claude-3:
    limits:
      tpm:
        capacity: 200000

entities:
  user-123:
    resources:
      gpt-4:
        limits:
          rpm:
            capacity: 500
      _default_:
        limits:
          rpm:
            capacity: 200
```

### Limit Shorthand

Only `capacity` is required. Defaults match `Limit.per_minute()`:

| Field | Default |
|-------|---------|
| `burst` | `capacity` |
| `refill_amount` | `capacity` |
| `refill_period` | `60` |

Limits are keyed by name (map, not list) to prevent duplicate limit names.

## State Tracking

A DynamoDB record tracks which items are managed by the YAML file:

| Field | Type | Description |
|-------|------|-------------|
| PK | `{ns}/SYSTEM#` | Namespace-scoped system key |
| SK | `#PROVISIONER` | Fixed sort key |
| `managed_system` | Boolean | Whether system defaults are managed |
| `managed_resources` | List[str] | Resource names managed by this manifest |
| `managed_entities` | Map[str, List[str]] | Entity ID to resource list mapping |
| `last_applied` | String | ISO timestamp of last apply |
| `applied_hash` | String | SHA-256 of YAML content |

### Terraform-style Reconciliation

| Scenario | Behavior |
|----------|----------|
| In YAML, not in DynamoDB | **Create** it |
| In YAML, in DynamoDB (managed) | **Update** if changed |
| Removed from YAML, was managed | **Delete** it |
| In DynamoDB, never in YAML | **Leave it alone** |

### Known Limitation

The `#PROVISIONER` record is a single DynamoDB item (400KB max). This limits managed entities to ~5,000 per namespace. Solvable later with sharding or item-level tagging if needed.

## Apply Algorithm

```
apply(manifest: LimitsManifest, repo: Repository):
  1. Auto-register namespace if it doesn't exist
  2. Read current #PROVISIONER record (or empty if first apply)
  3. Compute diff:

     SYSTEM:
       manifest has system + previous didn't    --> set_system_defaults()
       manifest has system + previous did        --> set_system_defaults() (overwrite)
       manifest lacks system + previous had      --> delete_system_defaults()

     RESOURCES:
       in manifest, not in previous_managed      --> set_resource_defaults()
       in manifest, in previous_managed          --> set_resource_defaults() (overwrite)
       not in manifest, in previous_managed      --> delete_resource_defaults()
       not in manifest, not in previous_managed  --> skip (unmanaged)

     ENTITIES (same pattern):
       in manifest, not in previous_managed      --> set_limits()
       in manifest, in previous_managed          --> set_limits() (overwrite)
       not in manifest, in previous_managed      --> delete_limits()
       not in manifest, not in previous_managed  --> skip (unmanaged)

  4. Write updated #PROVISIONER record with new managed set + hash
```

**Plan mode** runs steps 1-3 and returns the diff without applying.

## CLI Commands

```bash
# Preview changes (like terraform plan)
zae-limiter limits plan -f tenant-alpha.limits.yaml

# Apply limits (idempotent, like terraform apply)
zae-limiter limits apply -f tenant-alpha.limits.yaml

# Detect out-of-band drift (compare YAML vs live DynamoDB state)
zae-limiter limits diff -f tenant-alpha.limits.yaml

# Generate a CloudFormation template from YAML
zae-limiter limits cfn-template -f tenant-alpha.limits.yaml
```

### Lambda Invocation

**Payload (CLI to Lambda):**

```json
{
  "action": "plan|apply",
  "manifest": {
    "namespace": "tenant-alpha",
    "system": { "on_unavailable": "allow", "limits": { "rpm": { "capacity": 10000 } } },
    "resources": { "gpt-4": { "limits": { "rpm": { "capacity": 1000 } } } },
    "entities": { "user-123": { "resources": { "gpt-4": { "limits": { "rpm": { "capacity": 500 } } } } } }
  }
}
```

**Response:**

```json
{
  "status": "applied|planned",
  "changes": [
    {"action": "create", "level": "resource", "target": "gpt-4", "limits": {"rpm": {"capacity": 1000}}},
    {"action": "update", "level": "entity", "target": "user-123/gpt-4", "limits": {"rpm": {"capacity": 500}}},
    {"action": "delete", "level": "resource", "target": "gpt-3.5-turbo"}
  ],
  "manifest_hash": "sha256:abc..."
}
```

## CloudFormation Integration

### Main Stack

The provisioner Lambda is added to the main zae-limiter stack with its ARN exported:

```yaml
LimitsProvisionerFunction:
  Type: AWS::Lambda::Function
  Properties:
    FunctionName: !Sub "${AWS::StackName}-limits-provisioner"
    Handler: zae_limiter_provisioner.handler.on_event

Outputs:
  LimitsProvisionerArn:
    Value: !GetAtt LimitsProvisionerFunction.Arn
    Export:
      Name: !Sub "${AWS::StackName}-LimitsProvisionerArn"
```

### Tenant Stack

Generated by `zae-limiter limits cfn-template`, one per namespace:

```yaml
Resources:
  TenantLimits:
    Type: Custom::ZaeLimiterLimits
    Properties:
      ServiceToken: !ImportValue "my-app-LimitsProvisionerArn"
      Namespace: tenant-alpha
      System:
        OnUnavailable: allow
        Limits:
          rpm:
            Capacity: 10000
      Resources:
        gpt-4:
          Limits:
            tpm:
              Capacity: 100000
```

### CFN Lifecycle

| CFN Event | Lambda Action |
|-----------|---------------|
| Create | `apply` (first apply, empty provisioner state) |
| Update | `apply` (diff against previous provisioner state) |
| Delete | `apply` with empty manifest (deletes all managed items) |

## Package Structure

```
src/zae_limiter_provisioner/
  __init__.py          # Re-exports handler, types
  handler.py           # Lambda entry: on_event() for CLI + CFN
  manifest.py          # LimitsManifest dataclass, YAML parsing, validation
  differ.py            # Diff engine: manifest vs #PROVISIONER record
  applier.py           # Applies changes via Repository API

src/zae_limiter/
  cli.py               # New `limits` command group (plan, apply, diff, cfn-template)
  schema.py            # New key builder: sk_provisioner()
  repository.py        # New methods: get_provisioner_state(), put_provisioner_state()
```

## Cost

| Operation | DynamoDB Cost |
|-----------|---------------|
| `limits plan` | Read-only: 1 RCU (state) + 1 RCU per level |
| `limits apply` (no changes) | 1 RCU (state) + 1 RCU per level + 1 WCU (state update) |
| `limits apply` (with changes) | 1 RCU (state) + 1 WCU per changed config + 1 WCU (state update) |

Administrative operation, not on the hot path -- cost is negligible.
