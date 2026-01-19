# Repository Interface Audit for Issue #150

**Date:** 2026-01-19
**Purpose:** Document all Repository methods used by RateLimiter and Lease

## Summary

From analysis of `limiter.py`, `lease.py`, and `repository.py`:
- **47 method/property accesses** from RateLimiter to Repository
- **2 method accesses** from Lease to Repository
- Repository has additional internal methods not exposed via protocol

## Properties Accessed

| Property | Used By | Line(s) |
|----------|---------|---------|
| `region: str \| None` | RateLimiter | 267, 268, 1176, 1279, 1343 |
| `endpoint_url: str \| None` | RateLimiter | 232, 250, 267, 268 |
| `stack_name: str` | Repository constructor | Internal |
| `table_name: str` | Repository constructor | Internal |

## Methods Used by RateLimiter

### Lifecycle Methods
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `close` | `async def close() -> None` | 283 |
| `ping` | `async def ping() -> bool` | 315 |

### Infrastructure (Extended - DynamoDB-specific)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `create_stack` | `async def create_stack(stack_options: StackOptions \| None) -> None` | 192 |

### Entity Operations (Core)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `create_entity` | `async def create_entity(entity_id, name, parent_id, metadata, principal) -> Entity` | 348 |
| `get_entity` | `async def get_entity(entity_id: str) -> Entity \| None` | 359, 655, 1113 |
| `delete_entity` | `async def delete_entity(entity_id, principal) -> None` | 374 |
| `get_children` | `async def get_children(parent_id: str) -> list[Entity]` | 379 |

### Bucket Operations (Core)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `get_bucket` | `async def get_bucket(entity_id, resource, limit_name) -> BucketState \| None` | 671 |
| `get_resource_buckets` | `async def get_resource_buckets(resource, limit_name) -> list[BucketState]` | 1107 |

### Transaction Operations (Core)
| Method | Signature | Used By |
|--------|-----------|---------|
| `build_bucket_put_item` | `def build_bucket_put_item(state, ttl_seconds=86400) -> dict` | Lease:173 |
| `transact_write` | `async def transact_write(items: list[dict]) -> None` | Lease:175 |

### Limit Config Operations (Core)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `set_limits` | `async def set_limits(entity_id, limits, resource, principal) -> None` | 933 |
| `get_limits` | `async def get_limits(entity_id, resource) -> list[Limit]` | 744, 951 |
| `delete_limits` | `async def delete_limits(entity_id, resource, principal) -> None` | 968 |

### Resource Defaults (Core)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `set_resource_defaults` | `async def set_resource_defaults(resource, limits, principal) -> None` | 991 |
| `get_resource_defaults` | `async def get_resource_defaults(resource) -> list[Limit]` | 749, 1007 |
| `delete_resource_defaults` | `async def delete_resource_defaults(resource, principal) -> None` | 1022 |
| `list_resources_with_defaults` | `async def list_resources_with_defaults() -> list[str]` | 1027 |

### System Defaults (Core)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `set_system_defaults` | `async def set_system_defaults(limits, on_unavailable, principal) -> None` | 1052 |
| `get_system_defaults` | `async def get_system_defaults() -> tuple[list[Limit], str \| None]` | 754, 791, 1064 |
| `delete_system_defaults` | `async def delete_system_defaults(principal) -> None` | 1081 |

### Version Management (Standard)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `get_version_record` | `async def get_version_record() -> dict[str, Any] \| None` | 211, 1329 |
| `set_version_record` | `async def set_version_record(schema_version, lambda_version, client_min_version, updated_by) -> None` | 252, 274 |

### Audit Logging (Extended)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `get_audit_events` | `async def get_audit_events(entity_id, limit, start_event_id) -> list[AuditEvent]` | 407 |

### Usage Snapshots (Extended)
| Method | Signature | Line(s) |
|--------|-----------|---------|
| `get_usage_snapshots` | `async def get_usage_snapshots(...) -> tuple[list[UsageSnapshot], dict \| None]` | 485 |
| `get_usage_summary` | `async def get_usage_summary(...) -> UsageSummary` | 542 |

## Methods Used by Lease

| Method | Signature | Line(s) |
|--------|-----------|---------|
| `build_bucket_put_item` | `def build_bucket_put_item(state, ttl_seconds) -> dict` | 173 |
| `transact_write` | `async def transact_write(items) -> None` | 175 |

## Protocol Design Decision

Per ADR-108, ADR-109, and ADR-110:

**Core Protocol Methods (required for all backends):**
- Entity: `get_entity`, `create_entity`, `delete_entity`, `get_children`
- Bucket: `get_bucket`, `build_bucket_put_item`, `transact_write`, `get_resource_buckets`
- Limits: `set_limits`, `get_limits`, `delete_limits`
- Resource defaults: `set_resource_defaults`, `get_resource_defaults`, `delete_resource_defaults`, `list_resources_with_defaults`
- System defaults: `set_system_defaults`, `get_system_defaults`, `delete_system_defaults`
- Version: `get_version_record`, `set_version_record`
- Lifecycle: `close`, `ping`

**Extended Methods (backend-specific, access via type narrowing):**
- Infrastructure: `create_stack` (DynamoDB only)
- Audit: `get_audit_events`
- Usage: `get_usage_snapshots`, `get_usage_summary`

**Note:** For this initial refactor, ALL methods used by RateLimiter will be included in the protocol to ensure existing functionality works. The capability-based separation (Core vs Extended) can be implemented in a future iteration.

## Full Method Signatures for Protocol

```python
@runtime_checkable
class RepositoryProtocol(Protocol):
    """Protocol for rate limiter data backends."""

    # Properties
    @property
    def region(self) -> str | None: ...

    @property
    def endpoint_url(self) -> str | None: ...

    @property
    def stack_name(self) -> str: ...

    @property
    def table_name(self) -> str: ...

    # Lifecycle
    async def close(self) -> None: ...
    async def ping(self) -> bool: ...

    # Infrastructure (DynamoDB-specific)
    async def create_stack(self, stack_options: StackOptions | None = None) -> None: ...

    # Entity operations
    async def create_entity(
        self,
        entity_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity: ...

    async def get_entity(self, entity_id: str) -> Entity | None: ...

    async def delete_entity(
        self,
        entity_id: str,
        principal: str | None = None,
    ) -> None: ...

    async def get_children(self, parent_id: str) -> list[Entity]: ...

    # Bucket operations
    async def get_bucket(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
    ) -> BucketState | None: ...

    async def get_resource_buckets(
        self,
        resource: str,
        limit_name: str | None = None,
    ) -> list[BucketState]: ...

    def build_bucket_put_item(
        self,
        state: BucketState,
        ttl_seconds: int = 86400,
    ) -> dict[str, Any]: ...

    async def transact_write(self, items: list[dict[str, Any]]) -> None: ...

    # Limit config operations
    async def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        resource: str = ...,
        principal: str | None = None,
    ) -> None: ...

    async def get_limits(
        self,
        entity_id: str,
        resource: str = ...,
    ) -> list[Limit]: ...

    async def delete_limits(
        self,
        entity_id: str,
        resource: str = ...,
        principal: str | None = None,
    ) -> None: ...

    # Resource defaults
    async def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
    ) -> None: ...

    async def get_resource_defaults(self, resource: str) -> list[Limit]: ...

    async def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None: ...

    async def list_resources_with_defaults(self) -> list[str]: ...

    # System defaults
    async def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: str | None = None,
        principal: str | None = None,
    ) -> None: ...

    async def get_system_defaults(self) -> tuple[list[Limit], str | None]: ...

    async def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None: ...

    # Version management
    async def get_version_record(self) -> dict[str, Any] | None: ...

    async def set_version_record(
        self,
        schema_version: str,
        lambda_version: str | None = None,
        client_min_version: str = "0.0.0",
        updated_by: str | None = None,
    ) -> None: ...

    # Audit logging
    async def get_audit_events(
        self,
        entity_id: str,
        limit: int = 100,
        start_event_id: str | None = None,
    ) -> list[AuditEvent]: ...

    # Usage snapshots
    async def get_usage_snapshots(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        next_key: dict[str, Any] | None = None,
    ) -> tuple[list[UsageSnapshot], dict[str, Any] | None]: ...

    async def get_usage_summary(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> UsageSummary: ...
```
