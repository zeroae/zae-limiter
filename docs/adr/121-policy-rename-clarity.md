# ADR-121: Rename IAM Policies for Clarity

**Status:** Accepted
**Date:** 2026-02-02
**Issue:** [#307](https://github.com/zeroae/zae-limiter/issues/307)
**Amends:** ADR-117

## Context

ADR-117 introduced three IAM managed policies: `AppPolicy`, `AdminPolicy`, and `ReadOnlyPolicy`. The naming implied a separation between "application" and "admin" operations, but this distinction was artificial:

- `AppPolicy` was intended for applications running `acquire()`, but couldn't create entities (`PutItem` missing)
- `AdminPolicy` was described as "for ops team managing config"

In practice, applications that create entities on-demand (e.g., for new users) need `AdminPolicy`, not `AppPolicy`. The middle ground of "can create entities but can't set limits" doesn't exist as a real use case.

## Decision

Rename policies to reflect their actual purpose:

| Old Name | New Name | Default Suffix | Purpose |
|----------|----------|----------------|---------|
| `AppPolicy` | `AcquireOnlyPolicy` | `-acq` | Minimal: `acquire()` path only |
| `AdminPolicy` | `FullAccessPolicy` | `-full` | All operations |
| `ReadOnlyPolicy` | `ReadOnlyPolicy` | `-read` | Read-only monitoring |

Additionally:
- Add `BatchGetItem` to `ReadOnlyPolicy` (it's a read operation)
- Add `Scan` and `DescribeTable` to `FullAccessPolicy` (true full access)

## Permission Matrix

| Action | ReadOnly | AcquireOnly | FullAccess |
|--------|:--------:|:-----------:|:----------:|
| `GetItem` | ✅ | ✅ | ✅ |
| `BatchGetItem` | ✅ | ✅ | ✅ |
| `Query` | ✅ | ✅ | ✅ |
| `Scan` | ✅ | ❌ | ✅ |
| `DescribeTable` | ✅ | ❌ | ✅ |
| `TransactWriteItems` | ❌ | ✅ | ✅ |
| `PutItem` | ❌ | ❌ | ✅ |
| `UpdateItem` | ❌ | ❌ | ✅ |
| `DeleteItem` | ❌ | ❌ | ✅ |
| `BatchWriteItem` | ❌ | ❌ | ✅ |

## Consequences

**Positive:**
- Names set correct expectations (no false promise of "app" vs "admin" split)
- `FullAccessPolicy` is truly full access (includes monitoring operations)
- `ReadOnlyPolicy` includes all read operations

**Negative:**
- Breaking change for users referencing old policy names/ARNs
- CloudFormation export names change (`-AppPolicyArn` → `-AcquireOnlyPolicyArn`)

## Alternatives Considered

### Keep old names with updated documentation
Rejected because: Names like "AppPolicy" actively mislead users about what the policy permits, regardless of documentation.

### Add a fourth policy tier for entity management
Rejected because: The "can create entities but can't set limits" use case doesn't exist in practice.
