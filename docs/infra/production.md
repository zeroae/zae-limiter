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
| Permission Boundary | `--permission-boundary ARN` | None | Use in restricted IAM environments |

### Example Production Deployment

```bash
zae-limiter deploy \
    --name prod-limiter \
    --region us-east-1 \
    --pitr-recovery-days 7 \
    --log-retention-days 90 \
    --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:alerts
```

## Security Best Practices

### Encryption

- DynamoDB uses AWS-managed keys (SSE-S3) by default
- For customer-managed keys (CMK), use CloudFormation template customization

### IAM

- Lambda aggregator uses least-privilege permissions:
    - `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query`
- Use `--permission-boundary` for restricted IAM environments
- Use `--role-name-format` for organizational naming policies

### Network

- No VPC required; uses AWS service endpoints
- For VPC deployment, configure VPC endpoints for DynamoDB and Lambda

### Secrets Management

- No secrets stored in DynamoDB
- Use AWS Secrets Manager or Parameter Store for API keys
- Rate limit entity IDs should not contain sensitive data

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

Costs scale with request volume (us-east-1 pricing):

| Volume | DynamoDB | Lambda | CloudWatch | Total |
|--------|----------|--------|------------|-------|
| 10K req/day | ~$0.50 | ~$0.20 | ~$0.10 | ~$1/month |
| 100K req/day | ~$5 | ~$2 | ~$1 | ~$8/month |
| 1M req/day | ~$45 | ~$12 | ~$5 | ~$62/month |

For detailed capacity planning and optimization, see [Performance Guide](../performance.md).

## Next Steps

- [Monitoring Guide](../monitoring.md) - Dashboards, alerts, Logs Insights
- [Performance Guide](../performance.md) - Capacity planning, optimization
- [Operations Guide](../operations/index.md) - Troubleshooting, recovery
