# ADR-109: Backend Capability Matrix

**Status:** Accepted
**Date:** 2026-01-19
**Issue:** [#150](https://github.com/zeroae/zae-limiter/issues/150)
**Milestone:** v0.5.0

## Context

The RepositoryProtocol extraction (#150) enables multiple storage backends (Redis #149, SQLite #156, In-Memory #157, Cosmos DB #158, Firestore #159, OCI NoSQL #160). Each backend has different capabilities. This ADR defines which features are required versus optional.

## Decision

Define a three-tier capability system:

| Tier | Requirement | Examples |
|------|-------------|----------|
| **Core** | Required for all backends | Token bucket, entity CRUD, transactions |
| **Standard** | Expected for production | Hierarchical limits, cascade, TTL |
| **Extended** | Backend-specific, optional | Audit logging, usage snapshots, infrastructure |

**Core features** (all backends MUST implement): entity CRUD, bucket operations, atomic transactions, limit configuration, lifecycle management.

**Standard features** (production backends SHOULD implement): hierarchical entities via `parent_id`, cascade support, TTL management, optimistic locking.

**Extended features** (declared via `BackendCapabilities`): audit logging, usage snapshots, infrastructure management, change streams, batch operations. Backends declare support via capability flags; RateLimiter checks before using.

### Capability Matrix

| Backend | Audit | Snapshots | Infra Mgmt | Streams | Batch |
|---------|-------|-----------|------------|---------|-------|
| DynamoDB | Yes | Yes | CloudFormation | Yes | Yes |
| Redis | Streams | Consumer | Manual | Yes | Pipeline |
| SQLite | Table | Polling | N/A | No | No |
| In-Memory | No | No | N/A | No | No |
| Cosmos DB | TBD | TBD | ARM/Terraform | Yes | Bulk |
| Firestore | TBD | TBD | Terraform | Yes | getAll |

See [#150](https://github.com/zeroae/zae-limiter/issues/150) for method signatures and implementation details.

## Consequences

**Positive:**
- Clear contract for backend implementers
- Users know what to expect from each backend
- Core features guaranteed; extensibility without breaking changes

**Negative:**
- Feature disparity between backends
- Documentation and testing matrix grows with each backend

## Alternatives Considered

### All features required
Rejected: Forces backends to implement unsuitable features (e.g., In-Memory with audit logging).

### No capability declaration
Rejected: Users can't discover available features.

### Separate protocols per tier
Rejected: Overly complex; single protocol with capabilities is cleaner.
