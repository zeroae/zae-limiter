# Troubleshooting Guide

This guide helps operators diagnose and resolve common issues with zae-limiter deployments.

## Overview

| Issue Category | Common Symptoms |
|----------------|-----------------|
| [Rate Limit Enforcement](#1-rate-limit-enforcement-failures) | Limits not enforced, unexpected `RateLimitExceeded` |
| [DynamoDB Throttling](#2-dynamodb-throttling) | `ProvisionedThroughputExceededException`, slow requests |
| [Lambda Aggregator](#3-lambda-aggregator-malfunctions) | Missing usage snapshots, DLQ messages |
| [Version Compatibility](#4-version-compatibility-errors) | `VersionMismatchError`, `IncompatibleSchemaError` |
| [Stream Processing](#5-stream-processing-lag) | High `IteratorAge`, delayed aggregation |
| [Recovery](#6-recovery-procedures) | Data corruption, failed migrations |

## 1. Rate Limit Enforcement Failures

### Symptoms

- Requests succeed when they should be rate limited
- `RateLimitExceeded` raised unexpectedly
- Cascade to parent entity not working
- Bucket state appears incorrect

### Diagnostic Steps

**Check entity and bucket state:**

```bash
# Query entity metadata
aws dynamodb get-item --table-name ZAEL-<name> \
  --key '{"PK": {"S": "ENTITY#<entity_id>"}, "SK": {"S": "#META"}}'

# Query bucket state for a specific limit
aws dynamodb get-item --table-name ZAEL-<name> \
  --key '{"PK": {"S": "ENTITY#<entity_id>"}, "SK": {"S": "#BUCKET#<resource>#<limit_name>"}}'
```

**Verify stored limits (if using `use_stored_limits=True`):**

```bash
aws dynamodb query --table-name ZAEL-<name> \
  --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
  --expression-attribute-values '{":pk": {"S": "ENTITY#<entity_id>"}, ":sk": {"S": "#LIMIT#"}}'
```

### Common Causes and Solutions

| Cause | Solution |
|-------|----------|
| **Entity not created** | Create entity before rate limiting: `await limiter.create_entity(...)` |
| **Wrong `use_stored_limits` setting** | Set `use_stored_limits=True` if limits are in DynamoDB |
| **Parent entity missing (cascade)** | Create parent entity and set `parent_id` on child |
| **Clock skew** | Ensure server time is synchronized (NTP) |
| **Stale bucket state** | Bucket refills over time; wait or manually reset |
| **Limit configuration mismatch** | Verify limit `capacity`, `burst`, and `refill_rate` match expectations |

### Cascade Not Working

If cascade to parent is not enforced:

1. Verify parent entity exists:
   ```bash
   aws dynamodb get-item --table-name ZAEL-<name> \
     --key '{"PK": {"S": "ENTITY#<parent_id>"}, "SK": {"S": "#META"}}'
   ```

2. Verify child has `parent_id` set:
   ```bash
   aws dynamodb get-item --table-name ZAEL-<name> \
     --key '{"PK": {"S": "ENTITY#<child_id>"}, "SK": {"S": "#META"}}'
   # Check the "parent_id" attribute in response
   ```

3. Ensure `cascade=True` in acquire call:
   ```python
   async with limiter.acquire(
       entity_id="child-id",
       cascade=True,  # Must be True to check parent
       ...
   ):
   ```

### Verification

```python
# Test rate limiting is working
from zae_limiter import RateLimiter, Limit, RateLimitExceeded

limiter = RateLimiter(name="<name>", region="<region>")

# Consume all capacity
for i in range(100):
    try:
        async with limiter.acquire(
            entity_id="test-entity",
            resource="test",
            limits=[Limit.per_minute("rpm", 100)],
            consume={"rpm": 1},
        ):
            pass
    except RateLimitExceeded as e:
        print(f"Rate limited after {i} requests, retry_after={e.retry_after_seconds}s")
        break
```

## 2. DynamoDB Throttling

### Symptoms

- `ProvisionedThroughputExceededException` errors
- Increased latency on rate limit checks
- CloudWatch throttle alarms triggered
- `RateLimiterUnavailable` with `FAIL_CLOSED` mode

### Diagnostic Steps

**Check CloudWatch metrics:**

```bash
# View throttle events (last hour)
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ReadThrottleEvents \
  --dimensions Name=TableName,Value=ZAEL-<name> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum
```

**Check capacity utilization:**

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=ZAEL-<name> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum
```

**Identify hot partitions (if Contributor Insights enabled):**

```bash
aws dynamodb describe-contributor-insights \
  --table-name ZAEL-<name>
```

### Common Causes and Solutions

| Cause | Solution |
|-------|----------|
| **Provisioned capacity too low** | Increase RCU/WCU or switch to on-demand |
| **Hot partition** | Distribute entity IDs more evenly |
| **Burst traffic** | Enable auto-scaling or use on-demand |
| **GSI throttling** | Check GSI capacity separately |

### Emergency Mitigation

**Switch to on-demand capacity (immediate):**

```bash
aws dynamodb update-table \
  --table-name ZAEL-<name> \
  --billing-mode PAY_PER_REQUEST
```

**Increase provisioned capacity:**

```bash
aws dynamodb update-table \
  --table-name ZAEL-<name> \
  --provisioned-throughput ReadCapacityUnits=1000,WriteCapacityUnits=500
```

### Capacity Planning

For detailed capacity calculations, see the [Performance Tuning Guide](performance.md#1-dynamodb-capacity-planning).

**Quick estimates:**
- Each `acquire()` call: ~2 RCU, ~4 WCU (with cascade: ~4 RCU, ~8 WCU)
- Each `available()` call: ~2 RCU, ~0 WCU

## 3. Lambda Aggregator Malfunctions

### Symptoms

- Usage snapshots not updating
- Messages accumulating in Dead Letter Queue (DLQ)
- Lambda duration alarm triggered
- CloudWatch Logs showing errors

### Diagnostic Steps

**Check Lambda errors:**

```bash
# View recent Lambda invocations
aws logs filter-log-events \
  --log-group-name /aws/lambda/ZAEL-<name>-aggregator \
  --start-time $(date -u -d '1 hour ago' +%s)000 \
  --filter-pattern "ERROR"
```

**Check DLQ message count:**

```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.<region>.amazonaws.com/<account>/ZAEL-<name>-aggregator-dlq \
  --attribute-names ApproximateNumberOfMessagesVisible
```

**Inspect DLQ messages:**

```bash
aws sqs receive-message \
  --queue-url https://sqs.<region>.amazonaws.com/<account>/ZAEL-<name>-aggregator-dlq \
  --max-number-of-messages 10 \
  --visibility-timeout 0
```

**Check Lambda duration:**

```sql
-- CloudWatch Logs Insights query
fields @timestamp, @message
| filter @message like /processing_time_ms/
| parse @message /processing_time_ms":(?<duration>[\d.]+)/
| filter duration > 40000
| sort @timestamp desc
| limit 20
```

### High Lambda Duration

**Symptoms:** Duration alarm triggered, `processing_time_ms` > 80% of timeout

**Diagnostic query:**

```sql
fields @timestamp, @message
| filter @message like /Batch processing completed/
| parse @message /processing_time_ms":(?<duration>[\d.]+)/
| stats avg(duration) as avg_ms,
        pct(duration, 50) as p50_ms,
        pct(duration, 95) as p95_ms,
        pct(duration, 99) as p99_ms
  by bin(1h)
| sort @timestamp desc
```

**Solutions:**

- Increase Lambda memory (CPU scales with memory)
- Reduce batch size in event source mapping
- Check DynamoDB latency metrics

**Adjust Lambda memory:**

```bash
aws lambda update-function-configuration \
  --function-name ZAEL-<name>-aggregator \
  --memory-size 512
```

### Messages in Dead Letter Queue

**Symptoms:** DLQ alarm triggered, messages accumulating

**Solutions:**

1. Fix the root cause (check Lambda logs for errors)
2. Reprocess DLQ messages after fix:

```python
import boto3
import json

sqs = boto3.client('sqs')
lambda_client = boto3.client('lambda')

dlq_url = "https://sqs.<region>.amazonaws.com/<account>/ZAEL-<name>-aggregator-dlq"

while True:
    response = sqs.receive_message(
        QueueUrl=dlq_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=5,
    )

    messages = response.get('Messages', [])
    if not messages:
        break

    for msg in messages:
        # Reprocess the failed event
        body = json.loads(msg['Body'])

        # Invoke Lambda directly with the failed records
        lambda_client.invoke(
            FunctionName='ZAEL-<name>-aggregator',
            InvocationType='Event',
            Payload=json.dumps(body),
        )

        # Delete from DLQ after successful reprocessing
        sqs.delete_message(
            QueueUrl=dlq_url,
            ReceiptHandle=msg['ReceiptHandle'],
        )
```

### Cold Start Issues

**Diagnostic query:**

```sql
fields @timestamp, @message, @duration
| filter @type = "REPORT"
| filter @message like /Init Duration/
| parse @message /Init Duration: (?<init_duration>[\d.]+) ms/
| stats count() as cold_starts,
        avg(init_duration) as avg_init_ms
  by bin(1h)
```

**Solutions:**
- Increase Lambda memory (faster initialization)
- Enable provisioned concurrency for consistent latency

## 4. Version Compatibility Errors

### Symptoms

- `VersionMismatchError` exception raised
- `IncompatibleSchemaError` exception raised
- CLI commands fail with version errors
- Rate limiter initialization fails

### Diagnostic Steps

**Check compatibility with CLI:**

```bash
zae-limiter check --name <name> --region <region>
```

**View detailed version information:**

```bash
zae-limiter version --name <name> --region <region>
```

**Query version record directly:**

```bash
aws dynamodb get-item --table-name ZAEL-<name> \
  --key '{"PK": {"S": "SYSTEM#"}, "SK": {"S": "#VERSION"}}'
```

### VersionMismatchError

**Cause:** Client library version differs from deployed Lambda version.

**Example error:**
```
VersionMismatchError: Version mismatch: client=1.2.0, schema=1.0.0, lambda=1.0.0.
Lambda update available.
```

**Solution:** Upgrade Lambda to match client:

```bash
zae-limiter upgrade --name <name> --region <region>
```

Or programmatically:

```python
from zae_limiter import RateLimiter, StackOptions

# Auto-update Lambda on initialization
limiter = RateLimiter(
    name="<name>",
    region="<region>",
    stack_options=StackOptions(),  # Enables auto-update
)
```

### IncompatibleSchemaError

**Cause:** Major version difference requiring schema migration.

**Example error:**
```
IncompatibleSchemaError: Incompatible schema: client 2.0.0 is not compatible
with schema 1.0.0. Migration required.
```

**Solution:** Follow the [Migration Guide](migrations.md) to upgrade the schema:

1. Create a backup
2. Run migration
3. Update client

```bash
# Create backup before migration
aws dynamodb create-backup \
  --table-name ZAEL-<name> \
  --backup-name "pre-migration-$(date +%Y%m%d)"
```

Then follow the migration procedures in the [Migration Guide](migrations.md#sample-migration-v200).

### Minimum Client Version Error

**Cause:** Infrastructure requires a newer client version.

**Solution:** Upgrade the client library:

```bash
pip install --upgrade zae-limiter
```

## 5. Stream Processing Lag

### Symptoms

- `IteratorAge` metric growing
- Usage snapshots delayed
- Stream iterator age alarm triggered
- Lambda throttling

### Diagnostic Steps

**Check IteratorAge metric:**

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name IteratorAge \
  --dimensions Name=FunctionName,Value=ZAEL-<name>-aggregator \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Maximum
```

**Check stream status:**

```bash
aws dynamodb describe-table --table-name ZAEL-<name> \
  --query 'Table.StreamSpecification'
```

**Check Lambda event source mapping:**

```bash
aws lambda list-event-source-mappings \
  --function-name ZAEL-<name>-aggregator
```

**Check Lambda concurrent executions:**

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name ConcurrentExecutions \
  --dimensions Name=FunctionName,Value=ZAEL-<name>-aggregator \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Maximum
```

### Common Causes and Solutions

| Cause | Solution |
|-------|----------|
| **Lambda errors** | Fix errors (check DLQ and logs) |
| **Lambda throttling** | Increase reserved concurrency |
| **Low Lambda concurrency** | Match concurrency to shard count |
| **DynamoDB throttling** | Increase table capacity |
| **Large batch sizes** | Reduce batch size in event source mapping |

### Increase Lambda Concurrency

```bash
# Set reserved concurrency
aws lambda put-function-concurrency \
  --function-name ZAEL-<name>-aggregator \
  --reserved-concurrent-executions 10
```

### Adjust Event Source Mapping

```bash
# Get current mapping UUID
MAPPING_UUID=$(aws lambda list-event-source-mappings \
  --function-name ZAEL-<name>-aggregator \
  --query 'EventSourceMappings[0].UUID' \
  --output text)

# Reduce batch size
aws lambda update-event-source-mapping \
  --uuid $MAPPING_UUID \
  --batch-size 50
```

### Stream Shard Scaling

DynamoDB Streams automatically scales shards based on table throughput. If you see many shards but low Lambda concurrency:

```bash
# Check shard count
aws dynamodbstreams describe-stream \
  --stream-arn $(aws dynamodb describe-table --table-name ZAEL-<name> \
    --query 'Table.LatestStreamArn' --output text) \
  --query 'StreamDescription.Shards | length(@)'
```

Ensure Lambda concurrency >= shard count for optimal processing.

## 6. Recovery Procedures

### DynamoDB Backup and Restore

**Create on-demand backup:**

```bash
aws dynamodb create-backup \
  --table-name ZAEL-<name> \
  --backup-name "manual-backup-$(date +%Y%m%d-%H%M%S)"
```

**List available backups:**

```bash
aws dynamodb list-backups \
  --table-name ZAEL-<name>
```

**Restore from backup:**

```bash
aws dynamodb restore-table-from-backup \
  --target-table-name ZAEL-<name>-restored \
  --backup-arn <backup-arn>
```

**Restore using Point-in-Time Recovery (PITR):**

```bash
# Check if PITR is enabled
aws dynamodb describe-continuous-backups \
  --table-name ZAEL-<name>

# Restore to specific point in time
aws dynamodb restore-table-to-point-in-time \
  --source-table-name ZAEL-<name> \
  --target-table-name ZAEL-<name>-restored \
  --restore-date-time "2024-01-15T10:00:00Z"
```

### Migration Rollback

For reversible migrations, use the rollback function:

```python
from zae_limiter.migrations import get_migrations
from zae_limiter.repository import Repository

async def rollback_migration(target_version: str):
    repo = Repository("ZAEL-<name>", "<region>", None)

    migrations = get_migrations()
    target = next((m for m in migrations if m.version == target_version), None)

    if target and target.reversible and target.rollback:
        await target.rollback(repo)
        print(f"Rolled back migration {target_version}")

        # Update version record
        await repo.set_version_record(
            schema_version="<previous_version>",
            updated_by="manual_rollback",
        )
    else:
        print("Migration is not reversible - restore from backup")

    await repo.close()
```

For non-reversible migrations, restore from backup taken before migration.

### Stack Redeployment

**Delete and recreate stack (preserves DynamoDB data with deletion protection):**

```bash
# First, check deletion protection
aws dynamodb describe-table --table-name ZAEL-<name> \
  --query 'Table.DeletionProtectionEnabled'

# Delete stack (table retained if deletion protection enabled)
zae-limiter delete --name <name> --region <region> --yes

# Redeploy
zae-limiter deploy --name <name> --region <region>
```

**Update existing stack:**

```bash
# Export template
zae-limiter cfn-template > updated-template.yaml

# Update via CloudFormation
aws cloudformation update-stack \
  --stack-name ZAEL-<name> \
  --template-body file://updated-template.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

### Data Reconciliation

**Reset a corrupted bucket:**

```bash
# Delete the bucket record (will be recreated on next acquire)
aws dynamodb delete-item --table-name ZAEL-<name> \
  --key '{"PK": {"S": "ENTITY#<entity_id>"}, "SK": {"S": "#BUCKET#<resource>#<limit_name>"}}'
```

**Reset all buckets for an entity:**

```bash
# Query all buckets
aws dynamodb query --table-name ZAEL-<name> \
  --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
  --expression-attribute-values '{":pk": {"S": "ENTITY#<entity_id>"}, ":sk": {"S": "#BUCKET#"}}' \
  --projection-expression "PK, SK"

# Delete each bucket (use batch-write-item for efficiency)
```

**Verify entity integrity:**

```python
async def verify_entity(limiter, entity_id: str):
    """Verify entity can perform rate limiting operations."""
    from zae_limiter import Limit

    try:
        # Check entity exists
        entity = await limiter.get_entity(entity_id)
        print(f"Entity: {entity.entity_id}, parent: {entity.parent_id}")

        # Check rate limiting works
        available = await limiter.available(
            entity_id=entity_id,
            resource="health-check",
            limits=[Limit.per_minute("test", 1000)],
        )
        print(f"Available capacity: {available}")

        return True
    except Exception as e:
        print(f"Entity verification failed: {e}")
        return False
```

## Quick Reference

### CLI Diagnostic Commands

| Command | Description |
|---------|-------------|
| `zae-limiter status --name <name>` | Check stack status and resources |
| `zae-limiter version --name <name>` | Show version information |
| `zae-limiter check --name <name>` | Check client/infrastructure compatibility |

### CloudWatch Metrics

| Metric | Namespace | Threshold | Description |
|--------|-----------|-----------|-------------|
| `Errors` | AWS/Lambda | > 1/5min | Lambda execution failures |
| `Duration` | AWS/Lambda | > 80% timeout | Processing time |
| `IteratorAge` | AWS/Lambda | > 30,000ms | Stream processing lag |
| `ReadThrottleEvents` | AWS/DynamoDB | > 0 | Read throttling |
| `WriteThrottleEvents` | AWS/DynamoDB | > 0 | Write throttling |
| `ApproximateNumberOfMessagesVisible` | AWS/SQS | > 0 | DLQ messages |

### Exception Reference

| Exception | Cause | Resolution |
|-----------|-------|------------|
| `RateLimitExceeded` | Rate limit violated | Wait `retry_after_seconds` or increase limit |
| `RateLimiterUnavailable` | DynamoDB unavailable | Check DynamoDB health, consider `FAIL_OPEN` |
| `EntityNotFoundError` | Entity doesn't exist | Create entity with `create_entity()` |
| `VersionMismatchError` | Client/Lambda version mismatch | Run `zae-limiter upgrade` |
| `IncompatibleSchemaError` | Major version difference | Follow migration guide |
| `StackCreationError` | CloudFormation failed | Check stack events for details |
| `ValidationError` | Invalid input | Check entity_id, resource, limit_name format |

### DynamoDB Key Patterns

| Pattern | Key | Description |
|---------|-----|-------------|
| Entity metadata | `PK=ENTITY#<id>, SK=#META` | Entity configuration |
| Bucket state | `PK=ENTITY#<id>, SK=#BUCKET#<resource>#<limit>` | Token bucket |
| Stored limit | `PK=ENTITY#<id>, SK=#LIMIT#<resource>#<limit>` | Limit config |
| Usage snapshot | `PK=ENTITY#<id>, SK=#USAGE#<resource>#<date>` | Aggregated usage |
| Version | `PK=SYSTEM#, SK=#VERSION` | Infrastructure version |

## Next Steps

- [Monitoring Guide](monitoring.md) - CloudWatch metrics, dashboards, and alerts
- [Performance Tuning](performance.md) - Capacity planning and optimization
- [Migration Guide](migrations.md) - Schema versioning and upgrades
- [Failure Modes](guide/failure-modes.md) - Configure behavior when DynamoDB is unavailable
