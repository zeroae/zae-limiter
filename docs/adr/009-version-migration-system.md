# ADR-009: Schema Version Migration System

**Status:** Accepted
**Date:** 2026-01-10
**PR:** [#15](https://github.com/zeroae/zae-limiter/pull/15)
**Milestone:** v0.1.0

## Context

As the library evolves, DynamoDB schema changes may be required. Without version tracking:

- Clients can't detect incompatible infrastructure
- No mechanism to run schema migrations
- Lambda and client code can drift out of sync
- Rolling updates are unsafe

## Decision

Implement version tracking in both DynamoDB and CloudFormation, with a migration framework for schema changes.

**Components:**

1. **Version record**: `PK=SYSTEM#, SK=#VERSION` stores current schema version
2. **Stack tags**: CloudFormation stack tagged with `SchemaVersion`
3. **Lambda env var**: `SCHEMA_VERSION` for runtime compatibility checks
4. **Migration registry**: Python framework for versioned migrations

**Client parameters:**
- `auto_update`: Auto-update Lambda on version mismatch (default: True, configured via `RepositoryBuilder.auto_update()`)

*Note: `strict_version` and `skip_version_check` were removed in v0.10.0 when version management moved from RateLimiter to Repository/RepositoryBuilder.*

## Consequences

**Positive:**
- Safe rolling updates: detect incompatibility before operations
- Automated Lambda updates when client library upgraded
- Migration framework ready for future schema changes
- Clear versioning across all components

**Negative:**
- Additional DynamoDB read on first operation (version check)
- Complexity in version comparison logic
- Migrations must be backward-compatible or coordinated

## Alternatives Considered

- **No versioning**: Rejected; silent failures on schema mismatch, unsafe upgrades
- **Stack tags only**: Rejected; doesn't help Lambda or clients detect issues at runtime
- **External version store (SSM)**: Rejected; adds dependency, latency, different IAM permissions
