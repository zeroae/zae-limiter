# Production Deployment

This guide covers production-readiness for zae-limiter deployments.

## Production Checklist

Before deploying to production:

| Feature | CLI Flag | Default | Recommendation |
|---------|----------|---------|----------------|
| Point-in-Time Recovery | `--pitr-recovery-days N` | Disabled | Enable (7-35 days) |
| CloudWatch Alarms | `--enable-alarms` | Enabled | Keep enabled |
| SNS Notifications | `--alarm-sns-topic ARN` | None | Configure for alerts |
| Log Retention | `--log-retention-days N` | 30 | 90+ for compliance |
| Audit Archival | `--enable-audit-archival` | Enabled | Keep enabled for compliance |
| Glacier Transition | `--audit-archive-glacier-days N` | 90 | Adjust based on access patterns |
| Permission Boundary | `--permission-boundary ARN` | None | Use in restricted IAM environments |
| X-Ray Tracing | `--enable-tracing` | Disabled | Enable for debugging/performance analysis |
| IAM Roles | `--create-iam-roles` | Disabled | Create App/Admin/ReadOnly roles |
| Skip IAM | `--no-iam` | Disabled | Skip all IAM resources for restricted environments |
| External Role | `--aggregator-role-arn ARN` | None | Use existing IAM role for Lambda |

### Example Production Deployment

```bash
zae-limiter deploy \
    --name prod-limiter \
    --region us-east-1 \
    --pitr-recovery-days 7 \
    --log-retention-days 90 \
    --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:alerts \
    --enable-audit-archival \
    --audit-archive-glacier-days 90
```

## Security Best Practices

### Encryption

- DynamoDB uses AWS-managed keys (SSE-S3) by default
- For customer-managed keys (CMK), use CloudFormation template customization

### IAM

#### Application Access Policies

The stack creates three IAM managed policies by default:

| Policy | Suffix | Use Case | When to Use |
|--------|--------|----------|-------------|
| **AcquireOnlyPolicy** | `-acq` | Applications calling `acquire()` | Production workloads, Lambda functions, ECS tasks |
| **FullAccessPolicy** | `-full` | Ops teams managing config | CLI tools, admin scripts, CI/CD pipelines |
| **ReadOnlyPolicy** | `-read` | Monitoring and dashboards | Grafana, CloudWatch dashboards, audit tools |

**Best practices:**

- **Attach AcquireOnlyPolicy for applications** - Provides only the permissions needed for rate limiting
- **Attach FullAccessPolicy for config management** - Separate from application credentials
- **Attach ReadOnlyPolicy for observability** - Safe access for monitoring systems
- **Disable with `--no-iam`** - When using existing IAM policies or cross-account access
- **Create roles with `--create-iam-roles`** - When you want pre-built roles that attach these policies

#### Lambda Aggregator

The Lambda aggregator processes DynamoDB Stream events for usage aggregation, proactive bucket refill, and audit archival. It uses a separate execution role with least-privilege permissions:

- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query`
- `s3:PutObject` (when audit archival is enabled)

#### Permission Boundaries

For restricted IAM environments:

- Use `--permission-boundary` to apply organizational policies to all created roles
- Use `--role-name-format` for organizational naming conventions

```bash
# Enterprise deployment with permission boundary
zae-limiter deploy \
    --name prod-limiter \
    --permission-boundary arn:aws:iam::123456789012:policy/ServiceBoundary \
    --role-name-format "svc-{}"
```

#### IAM Behavior Matrix

The following table shows what IAM resources are created based on flag combinations:

| Flag Combination | Policies | App Roles | Aggr Role | Lambda |
|------------------|----------|-----------|-----------|--------|
| (default) | Created | No | Created | Enabled |
| `--no-iam` | No | No | No | Disabled |
| `--no-iam --aggregator-role-arn` | No | No | External | Enabled |
| `--aggregator-role-arn` | Created | No | External | Enabled |
| `--create-iam-roles` | Created | Created | Created | Enabled |
| `--no-iam --create-iam-roles` | Error | - | - | - |

**For PowerUserAccess or similar restricted IAM environments:**

```bash
# Deploy without any IAM resources (aggregator disabled)
zae-limiter deploy --name myapp --no-iam

# Deploy with external Lambda role (aggregator enabled)
zae-limiter deploy --name myapp --no-iam \
    --aggregator-role-arn arn:aws:iam::123456789012:role/MyLambdaRole
```

#### Multi-Tenant Deployments

zae-limiter supports multi-tenant architectures through namespace isolation. All tenants share a single DynamoDB table, but each tenant's data is logically isolated by a namespace ID prefix on all partition keys. This provides:

- **Cost efficiency** — One table, one Lambda, one set of CloudWatch alarms
- **Isolation** — Namespace-scoped IAM policies prevent cross-tenant access via TBAC
- **Lifecycle** — Namespaces can be registered, soft-deleted, recovered, and purged independently

Use namespaces when you need per-tenant isolation without deploying separate stacks.

#### Namespace-Scoped Access Control

For multi-tenant deployments, namespace-scoped policies restrict each tenant to its own namespace's DynamoDB items using tag-based access control (TBAC).

The stack creates three additional namespace-scoped policies:

| Policy | Suffix | Use Case | Tag Required |
|--------|--------|----------|--------------|
| **NamespaceAcquirePolicy** | `-ns-acq` | Tenant apps calling `acquire()` | Yes |
| **NamespaceFullAccessPolicy** | `-ns-full` | Tenant config management | Yes |
| **NamespaceReadOnlyPolicy** | `-ns-read` | Tenant monitoring | Yes |

These policies use `dynamodb:LeadingKeys` with the `zael_namespace_id` principal tag to restrict DynamoDB access to items whose partition key starts with the tenant's namespace ID.

##### Single Namespace (Most Common)

Attach a namespace-scoped policy and tag the principal with the namespace ID:

```bash
# 1. Deploy the stack (policies are created automatically)
zae-limiter deploy --name shared-table --region us-east-1

# 2. Look up the default namespace ID
zae-limiter namespace show default --name shared-table
# Output:
# Namespace:    default
# Namespace ID: default
# Status:       active
# Created At:   2025-01-15T10:30:00Z

# 3. Attach the namespace-scoped policy to your app role
aws iam attach-role-policy --role-name my-app-role \
  --policy-arn arn:aws:iam::123456789012:policy/shared-table-ns-acq

# 4. Tag the role with the namespace ID
aws iam tag-role --role-name my-app-role \
  --tags Key=zael_namespace_id,Value=default
```

For additional tenants, register a namespace and use its ID:

```bash
# Register a new tenant namespace
zae-limiter namespace register tenant-alpha --name shared-table

# Look up the opaque namespace ID
zae-limiter namespace show tenant-alpha --name shared-table
# Namespace ID: a7x3kq2m

# Tag the tenant's role
aws iam tag-role --role-name tenant-alpha-role \
  --tags Key=zael_namespace_id,Value=a7x3kq2m
```

##### Admin / Cross-Namespace Access

For admin roles that need access to all namespaces, attach a table-level policy (no tag needed):

```bash
aws iam attach-role-policy --role-name admin-role \
  --policy-arn arn:aws:iam::123456789012:policy/shared-table-full
```

##### Selective Multi-Namespace Access

**Option A: STS Session Policy** — Assume a base role with a session policy that narrows access to specific namespaces:

```python
import json

import boto3

sts = boto3.client("sts")
ns_ids = ["a7x3kq2m", "b9y4lr3n"]  # resolved namespace IDs

response = sts.assume_role(
    RoleArn="arn:aws:iam::123456789012:role/shared-table-base-role",
    RoleSessionName="multi-tenant-session",
    Policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem", "dynamodb:BatchGetItem",
                "dynamodb:Query", "dynamodb:UpdateItem",
            ],
            "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/shared-table",
            "Condition": {
                "ForAllValues:StringLike": {
                    "dynamodb:LeadingKeys":
                        [f"{ns}/*" for ns in ns_ids] + ["_/*"]
                }
            }
        }]
    })
)
# Use response["Credentials"] for scoped access
```

The session policy intersects with the base role's table-level policy, narrowing access to exactly those namespaces.

**Option B: Custom IAM Policy** — Create a managed policy listing specific namespace IDs:

```bash
# Resolve namespace IDs
NSID1=$(zae-limiter namespace show tenant-alpha --name shared-table \
  | grep "Namespace ID:" | awk '{print $3}')
NSID2=$(zae-limiter namespace show tenant-beta --name shared-table \
  | grep "Namespace ID:" | awk '{print $3}')

# Create a custom policy for multiple namespaces
aws iam create-policy --policy-name my-multi-ns-policy \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [
        \"dynamodb:GetItem\", \"dynamodb:BatchGetItem\",
        \"dynamodb:Query\", \"dynamodb:UpdateItem\"
      ],
      \"Resource\": \"arn:aws:dynamodb:us-east-1:123456789012:table/shared-table\",
      \"Condition\": {
        \"ForAllValues:StringLike\": {
          \"dynamodb:LeadingKeys\": [\"${NSID1}/*\", \"${NSID2}/*\", \"_/*\"]
        }
      }
    }]
  }"
```

This policy is operator-managed (not owned by the zae-limiter CloudFormation stack).

### Network

- No VPC required; uses AWS service endpoints
- For VPC deployment, configure VPC endpoints for DynamoDB and Lambda

### Secrets Management

- No secrets stored in DynamoDB
- Use AWS Secrets Manager or Parameter Store for API keys
- Rate limit entity IDs should not contain sensitive data

### Audit Logging

- All entity and limit changes are automatically logged
- Track who made changes with optional `principal` parameter
- Events auto-expire after 90 days (configurable via `--audit-ttl-days`)
- **Expired events are archived to S3** for long-term retention
- Archives transition to Glacier IR after 90 days (configurable via `--audit-archive-glacier-days`)
- For compliance requirements, see [Audit Logging Guide](auditing.md)

## Multi-Region Considerations

zae-limiter is designed for **single-region deployment**:

| Scenario | Approach |
|----------|----------|
| Single region | Standard deployment |
| Multi-region (independent) | Deploy separate stacks per region |
| Global rate limiting | Application-level coordination required |

### Why Not Global Tables?

- Rate limit state is time-sensitive (token buckets refill continuously)
- Cross-region replication lag would cause inconsistent limits
- Each region should enforce its own limits

### Cross-Region Pattern

```python
# Connect to separate stacks per region
us_repo = await Repository.connect("prod", "us-east-1")
eu_repo = await Repository.connect("prod", "eu-west-1")

us_limiter = RateLimiter(repository=us_repo)
eu_limiter = RateLimiter(repository=eu_repo)

# Application coordinates between regions if needed
```

## Monitoring & Alerting

The stack deploys CloudWatch alarms by default:

| Alarm | Trigger | Action |
|-------|---------|--------|
| Lambda Errors | > 1 per 5 min | Check logs, verify DynamoDB access |
| Lambda Duration | > 80% timeout | Increase memory or timeout |
| Iterator Age | > 30 seconds | Check Lambda concurrency |
| DLQ Messages | >= 1 | Investigate failed records |
| DynamoDB Throttles | > 1 per 5 min | Review capacity planning |

For dashboard templates and Logs Insights queries, see [Monitoring Guide](../monitoring.md).

## Cost Estimation

Costs scale with request volume (us-east-1 pricing, v0.7.0+ O(1) costs):

| Volume | DynamoDB | Lambda | CloudWatch | S3 Archive | Total |
|--------|----------|--------|------------|------------|-------|
| 10K req/day | ~$0.25 | ~$0.20 | ~$0.10 | ~$0.01 | ~$0.56/month |
| 100K req/day | ~$2.50 | ~$2 | ~$1 | ~$0.10 | ~$5.60/month |
| 1M req/day | ~$22 | ~$12 | ~$5 | ~$1 | ~$40/month |

S3 costs include:
- **Standard storage**: First 90 days (or configured `--audit-archive-glacier-days`)
- **Glacier IR storage**: After transition (~80% cheaper than Standard)
- **PUT requests**: One per Lambda batch (~$0.005 per 1000 requests)

For detailed capacity planning and optimization, see [Performance Guide](../performance.md).

## Next Steps

- [Monitoring Guide](../monitoring.md) - Dashboards, alerts, Logs Insights
- [Audit Logging](auditing.md) - Compliance tracking and incident investigation
- [Performance Guide](../performance.md) - Capacity planning, optimization
- [Operations Guide](../operations/index.md) - Troubleshooting, recovery
