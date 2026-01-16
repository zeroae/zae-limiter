# Audit Logging

zae-limiter provides built-in audit logging for security-sensitive operations, enabling compliance tracking, troubleshooting, and incident investigation.

## Overview

The audit system automatically logs:

| Action | Trigger | Details Captured |
|--------|---------|------------------|
| `entity_created` | Creating an entity | name, parent_id, metadata |
| `entity_deleted` | Deleting an entity | number of records deleted |
| `limits_set` | Configuring limits | all limit configurations |
| `limits_deleted` | Removing limits | resource name |

Each audit event includes:

- **Unique event ID** - ULID (time-sortable, collision-free)
- **Timestamp** - ISO 8601 UTC
- **Entity ID** - The affected entity
- **Principal** - Who performed the action (optional)
- **Details** - Action-specific context

## Audit Event Structure

```python
from zae_limiter import AuditEvent, AuditAction

# Example audit event
event = AuditEvent(
    event_id="01HQXYZ123ABC456DEF789GHI",
    timestamp="2024-01-15T10:30:00.000000+00:00",
    action=AuditAction.LIMITS_SET,
    entity_id="api-key-123",
    principal="admin@example.com",
    resource="gpt-4",
    details={
        "limits": [
            {"name": "rpm", "capacity": 100, "burst": 150}
        ]
    }
)
```

### AuditAction Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `ENTITY_CREATED` | `"entity_created"` | New entity was created |
| `ENTITY_DELETED` | `"entity_deleted"` | Entity was deleted |
| `LIMITS_SET` | `"limits_set"` | Limits were configured |
| `LIMITS_DELETED` | `"limits_deleted"` | Limits were removed |

## Principal Tracking

Track who performed each action by passing the `principal` parameter to entity and limit management methods:

```python
from zae_limiter import RateLimiter, Limit

limiter = RateLimiter(name="limiter", region="us-east-1")

# Track who created the entity
await limiter.create_entity(
    entity_id="api-key-123",
    name="Production Key",
    principal="admin@example.com",  # Tracks the caller
)

# Track who configured limits
await limiter.set_limits(
    entity_id="api-key-123",
    limits=[Limit.per_minute("rpm", 100)],
    principal="ops-team@example.com",
)

# Track who deleted limits
await limiter.delete_limits(
    entity_id="api-key-123",
    resource="gpt-4",
    principal="ops-team@example.com",
)

# Track who deleted the entity
await limiter.delete_entity(
    entity_id="api-key-123",
    principal="admin@example.com",
)
```

### Auto-Detection of AWS Caller Identity

When `principal` is not provided, zae-limiter automatically detects the AWS caller identity (ARN) using STS `GetCallerIdentity`. This means audit events automatically capture who made changes without requiring explicit principal tracking:

```python
# No principal specified - AWS ARN is auto-detected
await limiter.create_entity(
    entity_id="api-key-123",
    name="Production Key",
)
# Audit event will have principal like:
# "arn:aws:iam::123456789012:user/admin"
# or "arn:aws:sts::123456789012:assumed-role/MyRole/session"
```

!!! tip "Best Practice"
    For human-readable audit trails, explicitly pass a `principal` that identifies the user or service (e.g., email address or service name). Auto-detection is useful as a fallback when the caller identity is not available at the application level.

**Valid principal formats:**

- Email: `user@example.com`
- Service: `auth-service-v2`
- Any identifier: alphanumeric start, then alphanumeric/underscore/hyphen/dot/colon/@

## Querying Audit Events

Retrieve audit events for an entity using the `get_audit_events()` method:

```python
from zae_limiter import RateLimiter

limiter = RateLimiter(name="limiter", region="us-east-1")

# Get recent audit events (most recent first)
events = await limiter.get_audit_events(
    entity_id="api-key-123",
    limit=100,
)

for event in events:
    print(f"{event.timestamp}: {event.action} by {event.principal}")
```

### Synchronous API

For synchronous code, use `SyncRateLimiter`:

```python
from zae_limiter import SyncRateLimiter

limiter = SyncRateLimiter(name="limiter", region="us-east-1")

events = limiter.get_audit_events(entity_id="api-key-123", limit=100)
for event in events:
    print(f"{event.timestamp}: {event.action} by {event.principal}")
```

### Pagination

Use `start_event_id` for pagination through large result sets:

```python
# First page
events = await limiter.get_audit_events(
    entity_id="api-key-123",
    limit=50,
)

# Next page (use last event's ID)
if events:
    more_events = await limiter.get_audit_events(
        entity_id="api-key-123",
        limit=50,
        start_event_id=events[-1].event_id,
    )
```

### CLI Access

Query audit events from the command line:

```bash
# List audit events for an entity
zae-limiter audit list --name limiter --entity-id api-key-123

# Limit results
zae-limiter audit list --entity-id api-key-123 --limit 10

# Paginate
zae-limiter audit list --entity-id api-key-123 --start-event-id 01HXYZ...
```

## Retention and TTL

Audit events auto-expire after **90 days** by default. This is configurable via the `ttl_seconds` parameter when logging events.

DynamoDB TTL handles deletion automatically:

- Events are marked with an expiration timestamp
- DynamoDB deletes expired items within 48 hours of TTL
- No manual cleanup required

!!! tip "Long-term Retention"
    For compliance requirements beyond 90 days, see [Planned Capabilities](#planned-capabilities) for S3 archival.

## DynamoDB Access Patterns

Audit events are stored in the same DynamoDB table with the following schema:

| Key | Format | Description |
|-----|--------|-------------|
| PK | `AUDIT#{entity_id}` | Groups events by entity |
| SK | `#AUDIT#{event_id}` | Sorts by event ID (chronological) |

### Direct DynamoDB Queries

For advanced use cases, query audit events directly:

```python
import boto3

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("ZAEL-limiter")

# Query all audit events for an entity
response = table.query(
    KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
    ExpressionAttributeValues={
        ":pk": "AUDIT#api-key-123",
        ":sk": "#AUDIT#",
    },
    ScanIndexForward=False,  # Most recent first
    Limit=100,
)

for item in response["Items"]:
    data = item["data"]
    print(f"{data['timestamp']}: {data['action']}")
```

## Use Cases

### Compliance Auditing

Answer "who changed what, when?" for SOC2, HIPAA, or internal audits:

```python
from zae_limiter import RateLimiter

limiter = RateLimiter(name="limiter", region="us-east-1")

# Find all changes to a specific entity
events = await limiter.get_audit_events(entity_id="sensitive-api-key")

for event in events:
    print(f"""
    Time: {event.timestamp}
    Action: {event.action}
    By: {event.principal or 'unknown'}
    Details: {event.details}
    """)
```

### Troubleshooting

Investigate when limits were changed:

```python
from zae_limiter import RateLimiter, AuditAction

limiter = RateLimiter(name="limiter", region="us-east-1")

# Filter for limit changes
events = await limiter.get_audit_events(entity_id="api-key-123")
limit_changes = [e for e in events if e.action in (
    AuditAction.LIMITS_SET,
    AuditAction.LIMITS_DELETED,
)]

for event in limit_changes:
    print(f"{event.timestamp}: {event.action}")
    if event.details.get("limits"):
        for limit in event.details["limits"]:
            print(f"  - {limit['name']}: {limit['capacity']}")
```

### Security Incident Response

Track entity deletions during an incident window:

```python
from zae_limiter import RateLimiter, AuditAction

limiter = RateLimiter(name="limiter", region="us-east-1")

events = await limiter.get_audit_events(entity_id="compromised-key")
deletions = [
    e for e in events
    if e.action == AuditAction.ENTITY_DELETED
]

for event in deletions:
    print(f"Deleted at {event.timestamp} by {event.principal}")
```

## Planned Capabilities

### S3 Archival

[Issue #77](https://github.com/zeroae/zae-limiter/issues/77) - Archive expired audit events to S3 for long-term retention:

- Lambda automatically archives TTL-deleted records
- S3 storage with date partitioning (`/audit/year=YYYY/month=MM/`)
- Query with Athena for historical analysis
- Target: v1.1.0

## Next Steps

- [Production Deployment](production.md) - Security best practices
- [Monitoring](../monitoring.md) - Observability and alerting
- [API Reference](../api/models.md#auditevent) - AuditEvent and AuditAction details
