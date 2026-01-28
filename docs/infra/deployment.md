# Deployment

This guide covers deploying zae-limiter infrastructure to AWS.

## Overview

zae-limiter uses CloudFormation to deploy:

- **DynamoDB Table** - Stores rate limit state, entities, and usage data
- **DynamoDB Streams** - Captures changes for usage aggregation
- **Lambda Function** - Aggregates usage into hourly/daily snapshots and archives audit events
- **S3 Bucket** - Archives expired audit events (when audit archival is enabled)
- **IAM Roles** - Least-privilege access for Lambda and application access (App/Admin/ReadOnly)
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
| `--name` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom endpoint (LocalStack) | None |
| `--enable-aggregator/--no-aggregator` | Deploy Lambda aggregator | `true` |
| `--log-retention-days` | CloudWatch log retention | `30` |
| `--pitr-recovery-days` | Point-in-time recovery (1-35 days) | None (disabled) |
| `--enable-audit-archival/--no-audit-archival` | Archive expired audit events to S3 | `true` |
| `--audit-archive-glacier-days` | Days before Glacier IR transition | `90` |
| `--enable-iam-roles/--no-iam-roles` | Create App/Admin/ReadOnly IAM roles | `true` |

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

## Admin vs Application Workflow

zae-limiter supports separation of concerns between infrastructure administrators and application developers.

### Admin Workflow (Infrastructure & Config)

Admins deploy infrastructure and configure rate limits centrally:

```bash
# 1. Deploy infrastructure
zae-limiter deploy --name prod --region us-east-1

# 2. Configure system-wide defaults
zae-limiter system set-defaults -l rpm:100 -l tpm:10000 --on-unavailable block

# 3. Configure resource-specific limits
zae-limiter resource set-defaults gpt-4 -l rpm:50 -l tpm:100000
zae-limiter resource set-defaults gpt-3.5-turbo -l rpm:200 -l tpm:500000

# 4. Configure premium user tiers
zae-limiter entity set-limits user-premium --resource gpt-4 -l rpm:500 -l tpm:500000
```

### Application Workflow (Connect Only)

Application code connects to existing infrastructure without managing it:

```python
from zae_limiter import RateLimiter

# Connect only - no stack_options means no infrastructure changes
limiter = RateLimiter(
    name="prod",
    region="us-east-1",
    # No stack_options = connect only, no create/update
)

# Limits are automatically resolved from stored config
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    limits=None,  # Auto-resolves: Entity > Resource > System
    consume={"rpm": 1},
) as lease:
    await call_api()
```

### Benefits

| Concern | Admin | Application |
|---------|-------|-------------|
| Infrastructure | ✓ Deploy, update, delete stacks | Connect only |
| Rate limits | ✓ Configure at all levels | Auto-resolved |
| Credentials | Full AWS access (or AdminRole) | DynamoDB read/write only (or AppRole) |
| Changes | Through CLI/IaC | None |

This separation allows:

- **Centralized control** - Admins manage limits without code changes
- **Simplified apps** - No hardcoded limits, automatic resolution
- **Audit trail** - All config changes logged to DynamoDB
- **Dynamic updates** - Change limits without redeploying apps

See [Configuration Hierarchy](../guide/config-hierarchy.md) for limit resolution details.

## Stack Lifecycle Management

### Programmatic Cleanup

In addition to the CLI, you can manage stack lifecycle programmatically using the `delete_stack()` method:

```python
from zae_limiter import RateLimiter, StackOptions

# Create stack
limiter = RateLimiter(
    name="limiter",
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
        name="dev",
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
    --stack-name limiter \
    --parameter-overrides \
        SnapshotRetentionDays=90 \
        EnablePITR=true \
    --capabilities CAPABILITY_NAMED_IAM

# Deploy Lambda code separately (function name is {stack-name}-aggregator)
zae-limiter lambda-export --output lambda.zip
aws lambda update-function-code \
    --function-name limiter-aggregator \
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
    name="limiter",
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

## Application IAM Roles

The stack creates three optional IAM roles for different access patterns. These are enabled by default and provide least-privilege access for applications, administrators, and monitoring systems.

### Enabling/Disabling Roles

=== "CLI"

    ```bash
    # Deploy with IAM roles (default)
    zae-limiter deploy --name limiter --region us-east-1

    # Deploy without IAM roles (for custom IAM)
    zae-limiter deploy --name limiter --region us-east-1 --no-iam-roles
    ```

=== "Programmatic"

    ```python
    from zae_limiter import RateLimiter, StackOptions

    # With IAM roles (default)
    limiter = RateLimiter(
        name="limiter",
        region="us-east-1",
        stack_options=StackOptions(),  # create_iam_roles=True by default
    )

    # Without IAM roles
    limiter = RateLimiter(
        name="limiter",
        region="us-east-1",
        stack_options=StackOptions(create_iam_roles=False),
    )
    ```

### Role Summary

| Role | Use Case | DynamoDB Permissions |
|------|----------|---------------------|
| `AppRole` | Applications calling `acquire()` | GetItem, Query, TransactWriteItems |
| `AdminRole` | Ops teams managing config | App + PutItem, DeleteItem, UpdateItem, BatchWriteItem |
| `ReadOnlyRole` | Monitoring and dashboards | GetItem, Query, Scan, DescribeTable |

### Viewing Role ARNs

The `status` command shows IAM role ARNs when roles are enabled:

```bash
zae-limiter status --name limiter --region us-east-1
```

Output includes:
```
IAM Roles
  App:           arn:aws:iam::123456789012:role/limiter-app-role
  Admin:         arn:aws:iam::123456789012:role/limiter-admin-role
  ReadOnly:      arn:aws:iam::123456789012:role/limiter-readonly-role
```

### Role Naming

- Default: `${StackName}-{app,admin,readonly}-role` (e.g., `limiter-app-role`)
- With `--role-name-format`: Custom naming pattern applied to all roles

Roles respect `--permission-boundary` if configured.

For detailed IAM role configuration and usage examples, see [CloudFormation - Application IAM Roles](cloudformation.md#application-iam-roles).

## Next Steps

- [Production](production.md) - Production checklist, security, cost estimation
- [CloudFormation](cloudformation.md) - Template details
- [Monitoring](../monitoring.md) - Dashboards, alerts, Logs Insights
- [LocalStack](../contributing/localstack.md) - Local development setup
