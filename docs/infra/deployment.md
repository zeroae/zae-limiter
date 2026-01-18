# Deployment

This guide covers deploying zae-limiter infrastructure to AWS.

## Overview

zae-limiter uses CloudFormation to deploy:

- **DynamoDB Table** - Stores rate limit state, entities, and usage data
- **DynamoDB Streams** - Captures changes for usage aggregation
- **Lambda Function** - Aggregates usage into hourly/daily snapshots and archives audit events
- **S3 Bucket** - Archives expired audit events (when audit archival is enabled)
- **IAM Roles** - Least-privilege access for Lambda
- **CloudWatch Logs** - Lambda function logs

## CLI Deployment (Recommended)

The simplest way to deploy:

```bash
# Deploy with defaults
zae-limiter deploy --name limiter --region us-east-1

# Deploy with custom settings
zae-limiter deploy \
    --name limiter \
    --region us-east-1 \
    --log-retention-days 90 \
    --pitr-recovery-days 7
```

### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier (creates ZAEL-{name} resources) | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom endpoint (LocalStack) | None |
| `--enable-aggregator/--no-aggregator` | Deploy Lambda aggregator | `true` |
| `--log-retention-days` | CloudWatch log retention | `30` |
| `--pitr-recovery-days` | Point-in-time recovery (1-35 days) | None (disabled) |
| `--enable-audit-archival/--no-audit-archival` | Archive expired audit events to S3 | `true` |
| `--audit-archive-glacier-days` | Days before Glacier IR transition | `90` |

For the full list of options, see the [CLI Reference](../cli.md#deploy).

### Check Stack Status

=== "CLI"

    ```bash
    zae-limiter status --name limiter --region us-east-1
    ```

=== "Programmatic"

    ```python
    from zae_limiter import RateLimiter

    limiter = RateLimiter(name="limiter", region="us-east-1")

    status = await limiter.get_status()  # Returns Status dataclass

    if not status.available:
        print("DynamoDB not reachable")
    elif status.stack_status == "CREATE_COMPLETE":
        print("Stack is ready")
        print(f"Schema version: {status.schema_version}")
        print(f"Table items: {status.table_item_count}")
    elif status.stack_status and "IN_PROGRESS" in status.stack_status:
        print(f"Operation in progress: {status.stack_status}")
    ```

### Discover Deployed Instances

List all zae-limiter stacks in a region:

=== "CLI"

    ```bash
    zae-limiter list --region us-east-1
    ```

    Output:
    ```
    Rate Limiter Instances (us-east-1)
    ===========================================================================================

    Name                 Status                    Version      Lambda       Schema     Created
    -------------------------------------------------------------------------------------------
    prod-api             ✓ CREATE_COMPLETE         0.2.0        0.2.0        1.0.0      2024-01-15
    staging              ✓ CREATE_COMPLETE         0.2.0        0.2.0        1.0.0      2024-01-10
    dev-test             ⏳ UPDATE_IN_PROGRESS     0.1.0        0.1.0        1.0.0      2023-12-01

    Total: 3 instance(s)
    ⚠️  1 instance(s) need attention
    ```

=== "Programmatic"

    ```python
    from zae_limiter import RateLimiter, LimiterInfo

    # List all deployed limiters in a region
    limiters = await RateLimiter.list_deployed(region="us-east-1")

    for limiter in limiters:
        if limiter.is_healthy:
            print(f"✓ {limiter.user_name}: {limiter.version}")
        elif limiter.is_failed:
            print(f"✗ {limiter.user_name}: {limiter.stack_status}")
        elif limiter.is_in_progress:
            print(f"⏳ {limiter.user_name}: {limiter.stack_status}")
    ```

The `LimiterInfo` object provides:

- `stack_name` / `user_name` - Full and user-friendly names
- `stack_status` - CloudFormation status
- `version` / `lambda_version` / `schema_version` - Version info from tags
- `is_healthy` / `is_in_progress` / `is_failed` - Status helpers

### Delete Stack

```bash
zae-limiter delete --name limiter --region us-east-1 --yes
```

## Stack Lifecycle Management

### Programmatic Cleanup

In addition to the CLI, you can manage stack lifecycle programmatically using the `delete_stack()` method:

```python
from zae_limiter import RateLimiter, StackOptions

# Create stack
limiter = RateLimiter(
    name="limiter",  # Creates ZAEL-limiter resources
    region="us-east-1",
    stack_options=StackOptions(),
)

# Use the limiter...
async with limiter:
    # Rate limiting operations here
    pass

# Delete stack when done
await limiter.delete_stack()
```

### Use-Case Guidance

#### Development and Prototyping

For rapid iteration, declare infrastructure with cleanup:

```python
async def dev_session():
    limiter = RateLimiter(
        name="dev",  # ZAEL-dev resources
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    try:
        async with limiter:
            # Development work...
            pass
    finally:
        # Clean up development stack
        await limiter.delete_stack()
```

#### Production

For production deployments, see the [Production Guide](production.md) covering:

- Production checklist (PITR, alarms, SNS)
- Security best practices
- Multi-region considerations
- Cost estimation

## CloudFormation Template

Export and customize the template:

```bash
# Export template
zae-limiter cfn-template > template.yaml

# Deploy with AWS CLI
aws cloudformation deploy \
    --template-file template.yaml \
    --stack-name ZAEL-limiter \
    --parameter-overrides \
        SnapshotRetentionDays=90 \
        EnablePITR=true \
    --capabilities CAPABILITY_NAMED_IAM

# Deploy Lambda code separately
zae-limiter lambda-export --output lambda.zip
aws lambda update-function-code \
    --function-name zae-limiter-aggregator \
    --zip-file fileb://lambda.zip
```

### Template Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `SnapshotWindows` | Aggregation windows | `hourly,daily` |
| `SnapshotRetentionDays` | Usage data retention | `90` |
| `EnablePITR` | Point-in-time recovery | `false` |
| `LogRetentionDays` | CloudWatch log retention | `14` |

## Programmatic Creation

Create infrastructure directly from your application:

```python
from zae_limiter import RateLimiter, StackOptions

limiter = RateLimiter(
    name="limiter",  # Creates ZAEL-limiter resources
    region="us-east-1",
    stack_options=StackOptions(
        snapshot_windows="hourly,daily",
        retention_days=90,
    ),
)
```

`StackOptions` declares the desired infrastructure state. CloudFormation ensures the
actual infrastructure matches your declaration—creating, updating, or leaving unchanged
as needed.

!!! note "Deployment Options"
    For organizations requiring strict infrastructure/application separation,
    use CLI deployment or CloudFormation template export. See the
    [Production Guide](production.md) for deployment recommendations.

## Infrastructure Details

### DynamoDB Table

- **Billing**: Pay-per-request (on-demand)
- **Encryption**: AWS-managed keys (default)
- **Streams**: NEW_AND_OLD_IMAGES for Lambda trigger

### DynamoDB Schema

| Key | Pattern | Purpose |
|-----|---------|---------|
| PK | `ENTITY#{id}` | Partition key |
| SK | `#META`, `#BUCKET#...`, `#LIMIT#...` | Sort key |
| GSI1PK | `PARENT#{id}` | Parent lookups |
| GSI2PK | `RESOURCE#{name}` | Resource aggregation |

### Lambda Function

- **Runtime**: Python 3.12
- **Memory**: 256 MB
- **Timeout**: 60 seconds
- **Trigger**: DynamoDB Streams

### IAM Permissions

The Lambda function has minimal permissions:

```yaml
- dynamodb:GetItem
- dynamodb:PutItem
- dynamodb:UpdateItem
- dynamodb:Query
```

When X-Ray tracing is enabled, additional permissions are granted:

```yaml
- xray:PutTraceSegments
- xray:PutTelemetryRecords
```

## AWS X-Ray Tracing

Enable X-Ray tracing to gain visibility into Lambda aggregator performance and troubleshoot issues.

### Enabling Tracing

=== "CLI"

    ```bash
    zae-limiter deploy --name limiter --region us-east-1 --enable-tracing
    ```

=== "Programmatic"

    ```python
    from zae_limiter import RateLimiter, StackOptions

    limiter = RateLimiter(
        name="limiter",
        region="us-east-1",
        stack_options=StackOptions(enable_tracing=True),
    )
    ```

### Key Details

| Aspect | Value |
|--------|-------|
| Default | Disabled (opt-in) |
| Tracing Mode | Active |
| IAM | Conditional (only when enabled) |

**Why Active Mode?**

The Lambda aggregator uses Active tracing mode rather than PassThrough because DynamoDB Streams do not propagate X-Ray trace context. Active mode ensures traces are always generated for Lambda invocations regardless of upstream trace headers.

### What You Get

With X-Ray tracing enabled, you can:

- **Trace Lambda cold starts** - Identify initialization latency
- **Monitor DynamoDB operations** - See query and update times
- **Debug failures** - Trace errors through the processing pipeline
- **Analyze performance** - Find bottlenecks in stream processing

### Cost Considerations

X-Ray charges based on traces recorded and retrieved. For typical usage:

- First 100,000 traces/month are free
- After free tier: $5.00 per million traces recorded

For high-volume deployments, consider sampling strategies or enabling tracing only for troubleshooting.

## Next Steps

- [Production](production.md) - Production checklist, security, cost estimation
- [CloudFormation](cloudformation.md) - Template details
- [Monitoring](../monitoring.md) - Dashboards, alerts, Logs Insights
- [LocalStack](../contributing/localstack.md) - Local development setup
