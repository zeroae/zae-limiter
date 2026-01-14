# Deployment

This guide covers deploying zae-limiter infrastructure to AWS.

## Overview

zae-limiter uses CloudFormation to deploy:

- **DynamoDB Table** - Stores rate limit state, entities, and usage data
- **DynamoDB Streams** - Captures changes for usage aggregation
- **Lambda Function** - Aggregates usage into hourly/daily snapshots
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

For the full list of options, see the [CLI Reference](../cli.md#deploy).

### Check Stack Status

```bash
zae-limiter status --name limiter --region us-east-1
```

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

## Next Steps

- [Production](production.md) - Production checklist, security, cost estimation
- [CloudFormation](cloudformation.md) - Template details
- [Monitoring](../monitoring.md) - Dashboards, alerts, Logs Insights
- [LocalStack](../contributing/localstack.md) - Local development setup
