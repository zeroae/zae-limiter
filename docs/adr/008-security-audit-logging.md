# ADR-008: Security Audit Logging

**Status:** Accepted
**Date:** 2026-01-12
**PR:** [#76](https://github.com/zeroae/zae-limiter/pull/76)
**Issue:** [#47](https://github.com/zeroae/zae-limiter/issues/47)
**Milestone:** v0.2.0

## Context

Rate limiting systems control access to resources, making them security-sensitive. Compliance requirements (SOC 2, HIPAA) and security best practices require audit trails for:

- Who changed rate limits and when
- Entity lifecycle (creation, deletion)
- Configuration modifications

Without audit logging, investigating security incidents or proving compliance is difficult.

## Decision

Store audit events in DynamoDB alongside rate limiting data, with automatic TTL-based expiration.

**Audit events logged:**

| Action | Trigger | Details |
|--------|---------|---------|
| `entity_created` | `create_entity()` | name, parent_id, metadata |
| `entity_deleted` | `delete_entity()` | records_deleted count |
| `limits_set` | `set_limits()` | resource, limits config |
| `limits_deleted` | `delete_limits()` | resource |

**Key structure:** `PK=AUDIT#{entity_id}, SK=#AUDIT#{timestamp}`

**Optional `principal` parameter** tracks who performed the action.

## Consequences

**Positive:**
- Full audit trail without external dependencies
- Same access patterns as other data (single table)
- Automatic cleanup via DynamoDB TTL (default 90 days)
- Principal tracking enables accountability

**Negative:**
- Storage cost for audit records (mitigated by TTL)
- No real-time alerting (would require separate system)
- Audit records deleted with TTL, not archived (see [#77](https://github.com/zeroae/zae-limiter/issues/77) for S3 archival)

## Alternatives Considered

- **CloudTrail only**: Rejected; doesn't capture application-level details (which limits changed)
- **External audit service**: Rejected; adds dependency, latency, cost
- **Separate audit table**: Rejected; loses transactional consistency with entity operations
