# ADR-007: Input Validation for Injection Prevention

**Status:** Accepted
**Date:** 2026-01-12
**PR:** [#75](https://github.com/zeroae/zae-limiter/pull/75)
**Issue:** [#48](https://github.com/zeroae/zae-limiter/issues/48)
**Milestone:** v0.2.0

## Context

DynamoDB keys use `#` as a delimiter (e.g., `ENTITY#user-1#BUCKET#gpt-4`). Without validation, malicious input containing `#` could:

- Traverse to different record types
- Access other entities' data
- Corrupt key structure

This is analogous to SQL injection but for DynamoDB key-based access patterns.

## Decision

Add comprehensive input validation at model construction time, forbidding the `#` delimiter character in all user-provided identifiers.

**Validation rules:**

| Field | Pattern | Max Length |
|-------|---------|------------|
| `entity_id`, `parent_id` | `^[a-zA-Z0-9][a-zA-Z0-9_.\-:@]*$` | 256 |
| `limit_name`, `resource` | `^[a-zA-Z][a-zA-Z0-9_.\-]*$` | 64 |

**New exceptions:**
- `ValidationError` (base)
- `InvalidIdentifierError` (entity_id, parent_id)
- `InvalidNameError` (limit_name, resource)

## Consequences

**Positive:**
- Injection attacks impossible by construction
- Early failure with clear error messages
- Supports common ID formats: UUIDs, API keys, emails
- Validation happens once at model creation, not on every operation

**Negative:**
- Some previously-valid IDs may be rejected (containing `#`)
- Migration required for existing data with invalid characters

## Alternatives Considered

- **Escape/encode special characters**: Rejected; complex, error-prone, obscures data in DynamoDB console
- **Validate only at repository layer**: Rejected; too late, models could be passed around with invalid data
- **Allowlist specific formats (UUID only)**: Rejected; too restrictive for diverse use cases
