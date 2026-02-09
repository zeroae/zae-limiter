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
# Deploy separate stacks per region
us_limiter = RateLimiter(name="prod", region="us-east-1")
eu_limiter = RateLimiter(name="prod", region="eu-west-1")

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
