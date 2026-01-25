# ADR-107: IAM Roles for Application Access

**Status:** Accepted
**Date:** 2026-01-19
**Issue:** [#132](https://github.com/zeroae/zae-limiter/issues/132)

## Context

Users deploying zae-limiter must create their own IAM policies to access the DynamoDB table. This requires understanding the exact DynamoDB actions needed for different use cases (applications vs. administrators vs. monitoring), leading to either overly permissive policies or trial-and-error debugging of permission errors.

The CloudFormation stack currently only creates the Lambda execution role for the aggregator. Different deployment patterns require different access levels: applications need transactional write access for `acquire()`, administrators need full CRUD for configuration management, and monitoring systems need read-only access.

## Decision

The CloudFormation stack creates three optional IAM roles (AppRole, AdminRole, ReadOnlyRole) with least-privilege DynamoDB permissions, enabled by default and controlled via `--no-iam-roles` flag or `StackOptions.create_iam_roles=False`.

## Consequences

**Positive:**
- Easy onboarding with correct IAM permissions out-of-the-box
- Least-privilege security enforced by default
- Clear separation between app/admin/monitoring access patterns
- Roles respect existing `permission_boundary` and `role_name_format` options
- No new IAM permission requirements (stack already needs `iam:CreateRole` for Lambda)

**Negative:**
- Three additional IAM roles per stack increases IAM resource count
- Users with existing IAM setup may have redundant roles (use `--no-iam-roles`)

## Alternatives Considered

### Inline IAM policies in documentation only
Rejected because: Users still need to create and maintain policies manually, defeating the goal of easy onboarding.

### Single role with configurable permissions
Rejected because: A single role can't satisfy least-privilege for different access patterns; applications shouldn't have config deletion rights.

### IAM policies attached to user-provided roles
Rejected because: Increases deployment complexity by requiring users to pre-create roles and pass ARNs as parameters.
