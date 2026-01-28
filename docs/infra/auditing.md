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

```{.python .lint-only}
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

```{.python .lint-only}
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

```{.python .lint-only}
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

```{.python .lint-only}
from zae_limiter import SyncRateLimiter

limiter = SyncRateLimiter(name="limiter", region="us-east-1")

events = limiter.get_audit_events(entity_id="api-key-123", limit=100)
for event in events:
    print(f"{event.timestamp}: {event.action} by {event.principal}")
```

### Pagination

Use `start_event_id` for pagination through large result sets:

```{.python .lint-only}
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
- Expired events are automatically archived to S3 (when enabled)

## S3 Archival

When audit archival is enabled (default), expired audit events are automatically archived to S3 before being deleted from DynamoDB. This enables long-term retention for compliance requirements.

### How It Works

1. **TTL Expiration**: DynamoDB marks audit events for deletion after 90 days
2. **Stream Trigger**: The Lambda aggregator receives TTL deletion events via DynamoDB Streams
3. **Archive to S3**: Events are written to S3 in compressed JSONL format
4. **Glacier Transition**: After configurable days (default: 90), archives transition to Glacier Instant Retrieval

### Configuration

#### CLI Deployment

```bash
# Default: archival enabled, 90-day Glacier transition
zae-limiter deploy --name limiter --region us-east-1

# Custom Glacier transition period
zae-limiter deploy --name limiter --audit-archive-glacier-days 180

# Disable archival entirely
zae-limiter deploy --name limiter --no-audit-archival
```

#### Programmatic Deployment

```python
from zae_limiter import RateLimiter, StackOptions

# With custom archival settings
limiter = RateLimiter(
    name="limiter",
    region="us-east-1",
    stack_options=StackOptions(
        enable_audit_archival=True,  # Default
        audit_archive_glacier_days=180,  # Custom transition period
    ),
)
```

### S3 Bucket Structure

Archives are stored in an S3 bucket with a name auto-generated by CloudFormation, with date-based partitioning for efficient querying. The bucket name is available in the CloudFormation stack outputs (`AuditArchiveBucketName`).

```
s3://<auto-generated-bucket-name>/
  audit/
    year=YYYY/
      month=MM/
        day=DD/
          audit-{request_id}-{timestamp}.jsonl.gz
```

Each file contains newline-delimited JSON records (gzip compressed):

```json
{"event_id": "01HQXYZ...", "action": "limits_set", "entity_id": "api-key-123", ...}
{"event_id": "01HQXYZ...", "action": "entity_created", "entity_id": "api-key-456", ...}
```

### S3 Bucket Security

The archive bucket is created with security best practices:

- **Server-side encryption**: AES256 (S3-managed keys)
- **Public access blocked**: All public access settings disabled
- **Lifecycle policy**: Automatic transition to Glacier Instant Retrieval

### Querying Archived Events

Use Amazon Athena to query archived audit events:

```sql
-- Create external table (one-time setup)
CREATE EXTERNAL TABLE audit_archive (
    event_id STRING,
    timestamp STRING,
    action STRING,
    entity_id STRING,
    principal STRING,
    resource STRING,
    details STRING
)
PARTITIONED BY (year STRING, month STRING, day STRING)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://<audit-archive-bucket-name>/audit/'
TBLPROPERTIES ('compressionType'='gzip');

-- Load partitions
MSCK REPAIR TABLE audit_archive;

-- Query audit events
SELECT * FROM audit_archive
WHERE year = '2024' AND month = '01'
AND action = 'limits_set';
```

### CloudFormation Resources

When audit archival is enabled, the following resources are created:

| Resource | Name Pattern | Purpose |
|----------|--------------|---------|
| S3 Bucket | `{stack-name}-audit-archive` | Archive storage |
| IAM Policy | (inline) | Lambda S3:PutObject permission |

### CloudFormation Outputs

| Output | Description |
|--------|-------------|
| `AuditArchiveBucketName` | S3 bucket name for archives |
| `AuditArchiveBucketArn` | S3 bucket ARN |

## DynamoDB Access Patterns

Audit events are stored in the same DynamoDB table with the following schema:

| Key | Format | Description |
|-----|--------|-------------|
| PK | `AUDIT#{entity_id}` | Groups events by entity |
| SK | `#AUDIT#{event_id}` | Sorts by event ID (chronological) |

### Direct DynamoDB Queries

For advanced use cases, query audit events directly:

```{.python .lint-only}
import boto3

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("limiter")

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

```{.python .lint-only}
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

```{.python .lint-only}
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

```{.python .lint-only}
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

## Next Steps

- [Production Deployment](production.md) - Security best practices
- [Monitoring](../monitoring.md) - Observability and alerting
- [API Reference](../api/models.md#auditevent) - AuditEvent and AuditAction details
