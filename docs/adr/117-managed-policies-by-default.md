# ADR-117: Managed Policies by Default

**Status:** Accepted
**Date:** 2026-01-31
**Issue:** [#272](https://github.com/zeroae/zae-limiter/issues/272)
**Supersedes:** ADR-107 (partial)

## Context

ADR-107 introduced IAM roles (AppRole, AdminRole, ReadOnlyRole) with inline policies as the default deployment option. However, this couples infrastructure to user IAM configuration: users who want to attach permissions to their own roles or users must either parse the CloudFormation template to extract policy documents, or accept redundant roles in their account.

Many enterprise environments require consistent IAM role naming conventions, use federated identity, or have existing roles that need rate limiter access. Creating managed policies instead of roles provides the same least-privilege permissions while giving users flexibility to attach policies to their preferred principals.

## Decision

The CloudFormation stack creates three IAM managed policies (AppPolicy, AdminPolicy, ReadOnlyPolicy) by default. IAM roles are opt-in via `--create-iam-roles` flag or `StackOptions.create_iam_roles=True`. When roles are created, they attach the managed policies rather than using inline policies.

## Consequences

**Positive:**
- Users can attach policies to existing roles, users, or federated identities
- Policies are always created, providing documentation of required permissions
- Roles remain available for simple deployments via opt-in flag
- No duplicate permission definitions (roles reference managed policies)
- Policy ARNs exported for easy integration with external tooling

**Negative:**
- Breaking change: existing deployments expecting roles must add `--create-iam-roles`
- Three IAM managed policies per stack regardless of whether roles are needed

## Alternatives Considered

### Keep roles as default, add policies as opt-in
Rejected because: Policies are universally useful (attach to any principal), while roles are only useful for simple deployments.

### Output policy documents in stack outputs instead of managed policies
Rejected because: JSON in outputs is hard to consume; managed policies integrate with IAM natively.
