# Deployment

This guide covers deploying zae-limiter infrastructure to AWS.

## Overview

zae-limiter uses CloudFormation to deploy:

- **DynamoDB Table** - Stores rate limit state, entities, and usage data
- **DynamoDB Streams** - Captures changes for usage aggregation
- **Lambda Function** - Aggregates usage into hourly/daily snapshots, proactively refills token buckets for active entities, and archives audit events
- **S3 Bucket** - Archives expired audit events (when audit archival is enabled)
- **IAM Policies** - Least-privilege managed policies (AcquireOnly/FullAccess/ReadOnly)
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
| `--usage-retention-days` | Usage snapshot retention | `90` |
| `--audit-retention-days` | Audit record retention in DynamoDB | `90` |
| `--pitr-recovery-days` | Point-in-time recovery (1-35 days) | None (disabled) |
| `--enable-audit-archival/--no-audit-archival` | Archive expired audit events to S3 | `true` |
| `--audit-archive-glacier-days` | Days before Glacier IR transition | `90` |
| `--enable-iam-roles/--no-iam-roles` | Create App/Admin/ReadOnly IAM roles | `true` |
| `--enable-deletion-protection/--no-deletion-protection` | Enable DynamoDB table deletion protection | `false` |

For the full list of options, see the [CLI Reference](../cli.md#deploy).

### Namespace Registration

Namespaces provide logical isolation within a single DynamoDB table. The `"default"` namespace is automatically registered by `zae-limiter deploy`, `Repository.open()`, and `Repository.builder().build()`. Application code then uses `Repository.open()` to connect to a registered namespace.

=== "CLI"

    ```bash
    # Register additional namespaces
    zae-limiter namespace register tenant-alpha --name limiter
    zae-limiter namespace register tenant-beta --name limiter

    # List namespaces
    zae-limiter namespace list --name limiter

    # Show namespace details
    zae-limiter namespace show tenant-alpha --name limiter
    ```

=== "Programmatic"

    ```python
    from zae_limiter import Repository, RateLimiter

    # Connect to a specific tenant namespace
    repo = await Repository.open("tenant-alpha")
    limiter = RateLimiter(repository=repo)

    # Register additional namespaces
    await repo.register_namespace("tenant-beta")
    ```

!!! info "Deploy is per-stack, not per-namespace"
    The `deploy` command creates the underlying infrastructure (DynamoDB table, Lambda, IAM policies). Namespaces are lightweight registry records within the table — registering a new namespace does not require a new deployment.

For namespace-scoped IAM access control, see [Namespace-Scoped Access Control](production.md#namespace-scoped-access-control).

### Check Stack Status

=== "CLI"

    ```bash
    zae-limiter status --name limiter --region us-east-1
    ```

=== "Programmatic"

    ```python
    from zae_limiter import Repository

    repo = await Repository.open()

    available = await repo.ping()  # Returns True if DynamoDB is reachable

    if available:
        print("Stack is ready")
    else:
        print("DynamoDB not reachable")
    ```

    For comprehensive status including CloudFormation details, use the CLI command.

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
from zae_limiter import Repository, RateLimiter

# Open repository (auto-provisions if needed)
repo = await Repository.open()
limiter = RateLimiter(repository=repo)

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
| Credentials | Full AWS access (or FullAccessPolicy) | DynamoDB read/write only (or AcquireOnlyPolicy) |
| Changes | Through CLI/IaC | None |

This separation allows:

- **Centralized control** - Admins manage limits without code changes
- **Simplified apps** - No hardcoded limits, automatic resolution
- **Audit trail** - All config changes logged to DynamoDB
- **Dynamic updates** - Change limits without redeploying apps

See [Configuration Hierarchy](../guide/config-hierarchy.md) for limit resolution details.

## Stack Lifecycle Management

### Programmatic Cleanup

In addition to the CLI, you can manage stack lifecycle programmatically using the Repository's `delete_stack()` method:

```python
from zae_limiter import Repository, RateLimiter

# Create repository with infrastructure
repo = await Repository.builder().build()

limiter = RateLimiter(repository=repo)

# Use the limiter...
async with limiter.acquire(
    entity_id="user-123",
    resource="api",
    consume={"requests": 1},
) as lease:
    pass

# Delete stack when done
await repo.delete_stack()
```

### Use-Case Guidance

#### Development and Prototyping

For rapid iteration, declare infrastructure with cleanup:

```python
from zae_limiter import Repository, RateLimiter

async def dev_session():
    repo = await (
        Repository.builder()
        .enable_aggregator(False)
        .build()
    )
    limiter = RateLimiter(repository=repo)

    try:
        # Development work...
        pass
    finally:
        # Clean up development stack
        await repo.delete_stack()
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
from zae_limiter import Repository, RateLimiter

repo = await (
    Repository.builder()
    .usage_retention_days(90)
    .build()
)
limiter = RateLimiter(repository=repo)
```

The builder declares the desired infrastructure state. CloudFormation ensures the
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
| PK | `{ns}/ENTITY#{id}` | Partition key |
| SK | `#META`, `#BUCKET#...`, `#LIMIT#...` | Sort key |
| GSI1PK | `{ns}/PARENT#{id}` | Parent lookups |
| GSI2PK | `{ns}/RESOURCE#{name}` | Resource aggregation |

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
    from zae_limiter import Repository, RateLimiter

    repo = await (
        Repository.builder()
        .enable_tracing(True)
        .build()
    )
    limiter = RateLimiter(repository=repo)
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

## Application IAM Policies

The stack creates three managed IAM policies by default for different access patterns. These provide least-privilege access for applications, administrators, and monitoring systems.

### Policy Summary

| Policy | Suffix | Use Case | DynamoDB Permissions |
|--------|--------|----------|---------------------|
| `AcquireOnlyPolicy` | `-acq` | Applications calling `acquire()` | GetItem, BatchGetItem, Query, TransactWriteItems |
| `FullAccessPolicy` | `-full` | Ops teams managing config | All of the above + PutItem, DeleteItem, UpdateItem, BatchWriteItem, Scan, DescribeTable |
| `ReadOnlyPolicy` | `-read` | Monitoring and dashboards | GetItem, BatchGetItem, Query, Scan, DescribeTable |

### Enabling/Disabling IAM Resources

=== "CLI"

    ```bash
    # Deploy with managed policies (default)
    zae-limiter deploy --name limiter --region us-east-1

    # Deploy with managed policies AND IAM roles
    zae-limiter deploy --name limiter --region us-east-1 --create-iam-roles

    # Deploy without any IAM resources (for restricted environments)
    zae-limiter deploy --name limiter --region us-east-1 --no-iam
    ```

=== "Programmatic"

    ```python
    from zae_limiter import Repository

    # With managed policies only (default)
    repo = await Repository.builder().build()

    # With managed policies AND IAM roles
    repo = await (
        Repository.builder()
        .create_iam_roles(True)
        .build()
    )

    # Without any IAM resources
    repo = await (
        Repository.builder()
        .create_iam(False)
        .build()
    )
    ```

### Viewing Policy ARNs

The `status` command shows IAM policy ARNs:

```bash
zae-limiter status --name limiter --region us-east-1
```

Output includes:
```
IAM Policies
  AcquireOnly:   arn:aws:iam::123456789012:policy/limiter-acq
  FullAccess:    arn:aws:iam::123456789012:policy/limiter-full
  ReadOnly:      arn:aws:iam::123456789012:policy/limiter-read
```

### Policy Naming

- Default: `${StackName}-{acq,full,read}` (e.g., `limiter-acq`)
- With `--policy-name-format`: Custom naming pattern applied to all policies

Policies respect `--permission-boundary` if configured.

For detailed IAM configuration and usage examples, see [CloudFormation - Application IAM Policies](cloudformation.md#application-iam-policies).

## Declarative Limits Management

Instead of configuring limits one at a time with `system set-defaults`, `resource set-defaults`, and `entity set-limits`, you can define all limits in a YAML manifest and apply them declaratively. A Lambda provisioner computes diffs and applies only the necessary changes.

### YAML Manifest

Create a `limits.yaml` file defining your desired limit configuration:

```yaml
namespace: default

system:
  on_unavailable: block
  limits:
    rpm:
      capacity: 1000
    tpm:
      capacity: 100000

resources:
  gpt-4:
    limits:
      rpm:
        capacity: 500
      tpm:
        capacity: 50000
  gpt-3.5-turbo:
    limits:
      rpm:
        capacity: 2000
      tpm:
        capacity: 500000

entities:
  user-premium:
    resources:
      gpt-4:
        limits:
          rpm:
            capacity: 1000
          tpm:
            capacity: 100000
```

**Limit fields:** Only `capacity` is required. When omitted, `refill_amount` defaults to `capacity` and `refill_period` defaults to `60` seconds. `capacity` is the bucket ceiling (max tokens). To customize:

```yaml
limits:
  rpm:
    capacity: 1000       # Max tokens (bucket ceiling)
    refill_amount: 100   # Refill 100 tokens per period
    refill_period: 6     # Every 6 seconds (= 1000/min)
```

### CLI Workflow

The typical workflow mirrors `terraform plan` / `terraform apply`:

```bash
# 1. Preview what would change
zae-limiter limits plan -n my-app -f limits.yaml

# 2. Apply the changes
zae-limiter limits apply -n my-app -f limits.yaml

# 3. Later, check for drift
zae-limiter limits diff -n my-app -f limits.yaml
```

The provisioner tracks which items it manages in a `#PROVISIONER` state record. When you remove an item from the manifest and re-apply, the provisioner deletes it from DynamoDB. Items created outside the manifest (via `set-defaults` or `set-limits` CLI commands) are not affected.

### Multi-Namespace

Each manifest targets a single namespace. To manage limits for multiple namespaces, use separate YAML files:

```bash
# Different limits per tenant
zae-limiter limits apply -n my-app -N tenant-alpha -f alpha-limits.yaml
zae-limiter limits apply -n my-app -N tenant-beta -f beta-limits.yaml
```

### CloudFormation Integration

Generate a CloudFormation template to manage limits as infrastructure-as-code:

```bash
# Generate the template
zae-limiter limits cfn-template -n my-app -f limits.yaml > limits-stack.yaml

# Deploy with AWS CLI
aws cloudformation deploy \
    --template-file limits-stack.yaml \
    --stack-name my-app-limits
```

The generated template creates a `Custom::ZaeLimiterLimits` resource backed by the provisioner Lambda. The Lambda ARN is imported from the main zae-limiter stack via `Fn::ImportValue`:

```yaml
Resources:
  TenantLimits:
    Type: Custom::ZaeLimiterLimits
    Properties:
      ServiceToken: !ImportValue my-app-ProvisionerArn
      TableName: my-app
      Namespace: default
      System:
        OnUnavailable: block
        Limits:
          rpm:
            Capacity: 1000
      Resources:
        gpt-4:
          Limits:
            rpm:
              Capacity: 500
```

This approach lets you manage limits alongside other infrastructure in CloudFormation, with full lifecycle support (Create, Update, Delete).

### Provisioner Architecture

The provisioner is a Lambda function (`{stack}-limits-provisioner`) that:

1. **Receives** either a CLI event (action + manifest) or a CloudFormation custom resource event
2. **Reads** the previous managed state from the `#PROVISIONER` DynamoDB record
3. **Computes** a diff between the manifest and previous state (create/update/delete changes)
4. **Applies** changes via DynamoDB PutItem (create/update) and DeleteItem (delete)
5. **Updates** the `#PROVISIONER` record with the new managed set and a SHA-256 hash of the applied manifest

The provisioner uses the same DynamoDB config records (system `#CONFIG`, resource `#CONFIG`, entity `#CONFIG#{resource}`) as the imperative API, so limits set declaratively are immediately visible to `acquire()` calls.

### Imperative vs Declarative

| Aspect | Imperative | Declarative |
|--------|-----------|-------------|
| Commands | `system set-defaults`, `resource set-defaults`, `entity set-limits` | `limits plan`, `limits apply` |
| State tracking | None (each command is independent) | `#PROVISIONER` record tracks managed items |
| Drift detection | Manual comparison | `limits diff` |
| Bulk changes | Multiple commands | Single YAML file |
| CFN integration | Not available | `limits cfn-template` |
| Best for | Ad-hoc changes, interactive use | Reproducible config, CI/CD pipelines |

Both approaches write to the same DynamoDB config records and can coexist. However, mixing imperative and declarative management for the same items may cause the provisioner to overwrite manual changes on the next apply.

## Next Steps

- [Production](production.md) - Production checklist, security, cost estimation
- [CloudFormation](cloudformation.md) - Template details
- [Monitoring](../monitoring.md) - Dashboards, alerts, Logs Insights
- [Namespace Keys Migration](../migrations/namespace-keys.md) - Migrating to namespace-prefixed keys
- [LocalStack](../contributing/localstack.md) - Local development setup
