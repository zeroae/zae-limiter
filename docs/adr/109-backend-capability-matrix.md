# ADR-109: Backend Capability Matrix

**Status:** Accepted
**Date:** 2026-01-19
**Issue:** [#150](https://github.com/zeroae/zae-limiter/issues/150)
**Milestone:** v0.5.0

## Context

The RepositoryProtocol extraction (#150) enables multiple storage backends beyond DynamoDB. Several backend implementations are planned:

- [#149](https://github.com/zeroae/zae-limiter/issues/149) - Redis (sub-millisecond latency)
- [#156](https://github.com/zeroae/zae-limiter/issues/156) - SQLite (local development)
- [#157](https://github.com/zeroae/zae-limiter/issues/157) - In-Memory (unit testing)
- [#158](https://github.com/zeroae/zae-limiter/issues/158) - Cosmos DB (Azure)
- [#159](https://github.com/zeroae/zae-limiter/issues/159) - Firestore (GCP)
- [#160](https://github.com/zeroae/zae-limiter/issues/160) - OCI NoSQL (Oracle Cloud)

Each backend has different capabilities based on the underlying technology. This ADR defines which features are required for all backends versus optional based on backend capabilities.

## Decision

Define a three-tier capability system:

| Tier | Requirement | Examples |
|------|-------------|----------|
| **Core** | Required for all backends | Token bucket, entity CRUD, transactions |
| **Standard** | Expected for production backends | Hierarchical limits, cascade, TTL |
| **Extended** | Backend-specific, optional | Audit logging, usage snapshots, infrastructure management |

### Feature Capability Matrix

| Feature | DynamoDB | Redis | SQLite | In-Memory | Cosmos DB | Firestore | OCI NoSQL |
|---------|----------|-------|--------|-----------|-----------|-----------|-----------|
| **Core Features** |
| Token bucket algorithm | Required | Required | Required | Required | Required | Required | Required |
| Entity CRUD | Required | Required | Required | Required | Required | Required | Required |
| Bucket CRUD | Required | Required | Required | Required | Required | Required | Required |
| Atomic transactions | Required | Required | Required | Required | Required | Required | Required |
| Limit configuration | Required | Required | Required | Required | Required | Required | Required |
| **Standard Features** |
| Hierarchical entities | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Cascade support | Yes | Lua script | Yes | Yes | TBD | Yes | TBD |
| TTL/expiration | Native | Native | Manual | Manual | Native | Native | Native |
| Optimistic locking | Yes | Yes | Yes | Yes | Yes | Yes | TBD |
| **Extended Features** |
| Audit logging | Native | Streams | Table | In-memory | Change Feed | Listeners | TBD |
| Usage snapshots | Lambda | Consumer | Polling | Skip | Functions | Functions | TBD |
| Infrastructure mgmt | CloudFormation | Manual | N/A | N/A | ARM/Terraform | Terraform | Terraform |
| Change streams | DynamoDB Streams | Redis Streams | N/A | N/A | Change Feed | Real-time | TBD |

### Core Features (Required)

All backends MUST implement these `RepositoryProtocol` methods:

```python
@runtime_checkable
class RepositoryProtocol(Protocol):
    # Entity operations
    async def get_entity(self, entity_id: str) -> Entity | None: ...
    async def create_entity(self, entity_id: str, name: str | None = None,
                           parent_id: str | None = None) -> Entity: ...
    async def delete_entity(self, entity_id: str) -> None: ...

    # Bucket operations
    async def get_buckets(self, entity_id: str, resource: str) -> list[BucketState]: ...
    async def get_or_create_bucket(self, entity_id: str, resource: str,
                                   limit: Limit) -> BucketState: ...

    # Transaction operations (atomic)
    async def transact_consume(self, entries: list[ConsumeEntry]) -> None: ...
    async def transact_release(self, entries: list[ReleaseEntry]) -> None: ...

    # Limit configuration
    async def get_limits(self, entity_id: str, resource: str) -> list[Limit]: ...
    async def set_limits(self, entity_id: str, limits: list[Limit],
                        resource: str) -> None: ...

    # Lifecycle
    async def close(self) -> None: ...
```

### Standard Features (Production Backends)

Production backends SHOULD implement:

1. **Hierarchical entities**: Parent-child relationships via `parent_id`
2. **Cascade support**: Check parent limits when `cascade=True`
3. **TTL management**: Automatic cleanup of expired records
4. **Optimistic locking**: Version-based conflict detection

### Extended Features (Backend-Specific)

Extended features are declared via capability flags:

```python
class BackendCapabilities:
    """Declares what extended features a backend supports."""
    supports_audit_logging: bool = False
    supports_usage_snapshots: bool = False
    supports_infrastructure_management: bool = False
    supports_change_streams: bool = False
    supports_batch_operations: bool = False

class RepositoryProtocol(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...
```

### Capability Declaration by Backend

| Backend | `audit_logging` | `usage_snapshots` | `infrastructure_management` | `change_streams` | `batch_operations` |
|---------|-----------------|-------------------|-----------------------------|--------------------|-------------------|
| DynamoDB | True | True | True | True | True |
| Redis | True (Streams) | True (Consumer) | False | True | True (Pipeline) |
| SQLite | True (Table) | True (Polling) | False | False | False |
| In-Memory | False | False | False | False | False |
| Cosmos DB | TBD | TBD | False | True | True (Bulk) |
| Firestore | TBD | TBD | False | True | True (getAll) |
| OCI NoSQL | TBD | TBD | False | TBD | TBD |

### Audit Logging Implementation by Backend

| Backend | Implementation | Notes |
|---------|----------------|-------|
| DynamoDB | Native table records | `PK=AUDIT#{entity_id}`, TTL-based retention |
| Redis | Redis Streams | `XADD audit:{entity_id} *` with `MAXLEN` |
| SQLite | Separate table | `audit_events` table with timestamp index |
| In-Memory | Not supported | Testing backend, no persistence |
| Cosmos DB | Change Feed consumer | Requires separate consumer process |
| Firestore | Real-time listeners | Requires Cloud Functions trigger |

### Usage Snapshots Implementation by Backend

| Backend | Implementation | Notes |
|---------|----------------|-------|
| DynamoDB | Lambda via DynamoDB Streams | Aggregates from `total_consumed_milli` delta |
| Redis | Separate consumer process | Consumes from Redis Streams |
| SQLite | Polling-based aggregation | Periodic queries, manual trigger |
| In-Memory | Not supported | Testing backend, skip snapshots |
| Cosmos DB | Azure Functions via Change Feed | Similar pattern to DynamoDB |
| Firestore | Cloud Functions via listeners | Similar pattern to DynamoDB |

### Infrastructure Management by Backend

| Backend | Tool | Notes |
|---------|------|-------|
| DynamoDB | CloudFormation | `StackOptions` for declarative infra |
| Redis | Manual | User manages Redis cluster |
| SQLite | N/A | File-based, no infrastructure |
| In-Memory | N/A | No persistence |
| Cosmos DB | ARM/Terraform | User manages via Azure tools |
| Firestore | Terraform | User manages via GCP tools |
| OCI NoSQL | Terraform | User manages via OCI tools |

## Consequences

**Positive:**
- Clear contract for backend implementers
- Users know what to expect from each backend
- Core features guaranteed across all backends
- Extensibility without breaking changes

**Negative:**
- Feature disparity between backends
- Documentation must cover per-backend differences
- Testing matrix grows with each backend

## Alternatives Considered

- **All features required**: Rejected; forces backends to implement features that don't fit their model (e.g., In-Memory with audit logging)
- **No capability declaration**: Rejected; users can't discover what features are available
- **Separate protocols per tier**: Rejected; overly complex, single protocol with capabilities is cleaner

## Related

- [ADR-001](001-single-table-dynamodb.md) - DynamoDB single-table design
- [ADR-010](010-total-consumed-counter.md) - Consumption tracking for snapshots
- [#150](https://github.com/zeroae/zae-limiter/issues/150) - Repository Protocol extraction
