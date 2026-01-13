# Plan: Troubleshooting Guide (Issue #36)

**Issue:** [#36 - docs: create troubleshooting guide](https://github.com/zeroae/zae-limiter/issues/36)
**Estimated Effort:** 3-4 hours
**Labels:** Documentation, v1.0.0

## Overview

Create a comprehensive troubleshooting guide (`docs/troubleshooting.md`) covering common operational problems with zae-limiter. The guide should help operators diagnose and resolve issues in production environments.

## Dependencies

- **#45** - Performance benchmarks (for verification procedures)
- **#46** - End-to-end integration tests (for validation steps)
- **#68** - Tracking issue for phase 3 documentation

These dependencies affect verification/validation procedures in the guide. The troubleshooting guide can be written independently but should reference benchmark and e2e test commands for validation.

## Consolidation Decision

**Decision:** Consolidate all troubleshooting content into `docs/troubleshooting.md`.

The `docs/monitoring.md` file currently contains a Troubleshooting section (lines 466-552) covering:
- High Lambda Duration
- Increasing Iterator Age
- Messages in Dead Letter Queue
- DynamoDB Throttling

**Action required:**
1. Migrate existing troubleshooting content from `monitoring.md` to `troubleshooting.md`
2. Expand migrated content with additional detail where needed
3. Replace `monitoring.md` Troubleshooting section with a link to `troubleshooting.md`
4. Keep `monitoring.md` focused on metrics, dashboards, alerts, and logs configuration

This consolidation ensures operators have a single reference for all troubleshooting scenarios.

## Target Document Structure

```
docs/troubleshooting.md
├── Introduction
├── 1. Rate Limit Enforcement Failures
│   ├── Symptom identification
│   ├── Diagnostic steps
│   ├── Common causes and solutions
│   └── Verification
├── 2. DynamoDB Throttling Issues
│   ├── Symptoms (ProvisionedThroughputExceededException)
│   ├── CloudWatch metrics to monitor
│   ├── Capacity planning fixes
│   └── Emergency mitigation
├── 3. Lambda Aggregator Malfunctions
│   ├── Dead Letter Queue monitoring
│   ├── CloudWatch Logs analysis
│   ├── Common failure patterns
│   └── Manual recovery steps
├── 4. Version Compatibility Errors
│   ├── Error types (VersionMismatchError, IncompatibleSchemaError)
│   ├── Diagnosis with CLI commands
│   ├── Upgrade/migration procedures
│   └── Rollback steps
├── 5. Stream Processing Lag
│   ├── Metrics to watch (IteratorAge)
│   ├── Shard analysis
│   ├── Scaling recommendations
│   └── Backpressure handling
├── 6. Recovery Procedures
│   ├── DynamoDB backup/restore
│   ├── Migration rollback
│   ├── Stack redeployment
│   └── Data reconciliation
└── Quick Reference
    ├── CLI diagnostic commands
    ├── CloudWatch metric names
    └── Error code reference
```

## Implementation Steps

### Step 1: Scaffold Document Structure

Create the basic document with section headers and introductory content.

**Tasks:**
- [ ] Create `docs/troubleshooting.md` with all section headers
- [ ] Write introduction explaining the guide's purpose
- [ ] Add table of contents with anchor links

**Existing resources to reference:**
- `docs/monitoring.md` (lines 466-552) - **migrate and expand** existing troubleshooting content
- `docs/guide/failure-modes.md` - for failure handling patterns
- `docs/performance.md` - for capacity/throttling context
- `docs/migrations.md` - for version/migration context

### Step 2: Rate Limit Enforcement Failures

Document scenarios where rate limits aren't enforced correctly.

**Content to cover:**
- Limits not being checked (configuration issues)
- Cascade not working (parent entity missing)
- Stored limits not loading (`use_stored_limits=False`)
- Bucket state corruption (clock skew, concurrent updates)
- `RateLimitExceeded` not raised when expected

**Code references:**
- `src/zae_limiter/exceptions.py:80` - `RateLimitExceeded` class
- `src/zae_limiter/bucket.py` - token bucket algorithm
- `src/zae_limiter/limiter.py` - acquire/available logic

**Diagnostic commands:**
```bash
# Check entity limits
zae-limiter status --name <name> --region <region>

# Verify bucket state (via DynamoDB queries)
aws dynamodb get-item --table-name ZAEL-<name> \
  --key '{"PK": {"S": "ENTITY#<id>"}, "SK": {"S": "#BUCKET#<resource>#<limit>"}}'
```

### Step 3: DynamoDB Throttling Issues

Document capacity-related problems and fixes.

**Migrate from `monitoring.md`:** "DynamoDB Throttling" section (lines 537-552)
- Existing content covers: symptoms, diagnostic steps (CloudWatch, Contributor Insights), solutions

**Content to cover (expand migrated content):**
- `ProvisionedThroughputExceededException` handling
- CloudWatch metrics: `ConsumedReadCapacityUnits`, `ConsumedWriteCapacityUnits`, `ThrottledRequests`
- On-demand vs provisioned capacity selection
- Auto-scaling configuration
- Burst capacity behavior

**Reference existing content:**
- `docs/performance.md` - Section 1 (DynamoDB Capacity Planning)

**Add troubleshooting-specific content:**
- How to identify which operation is causing throttling
- Emergency capacity increase procedures
- Cost implications of capacity changes

### Step 4: Lambda Aggregator Malfunctions

Document Lambda-specific operational issues.

**Migrate from `monitoring.md`:**
- "High Lambda Duration" section (lines 468-496) - symptoms, Logs Insights queries, solutions
- "Messages in Dead Letter Queue" section (lines 517-534) - symptoms, diagnostic steps, solutions

**Content to cover (expand migrated content):**
- Dead Letter Queue (DLQ) monitoring and processing
- CloudWatch Logs error patterns
- Lambda timeout issues (80% duration alarm)
- Memory exhaustion
- Cold start impacts
- Stream batch processing failures

**Code references:**
- `src/zae_limiter/aggregator/handler.py` - Lambda entry point
- `src/zae_limiter/aggregator/processor.py` - stream processing logic

**Diagnostic commands:**
```bash
# Check Lambda logs
aws logs tail /aws/lambda/ZAEL-<name>-aggregator --follow

# Check DLQ message count
aws sqs get-queue-attributes \
  --queue-url <dlq-url> \
  --attribute-names ApproximateNumberOfMessages

# Reprocess DLQ messages
# (document manual reprocessing procedure)
```

### Step 5: Version Compatibility Errors

Document version-related issues and resolution.

**Content to cover:**
- `VersionMismatchError` - client/infrastructure version differences
- `IncompatibleSchemaError` - major version incompatibilities
- Minimum client version enforcement
- Lambda version updates vs schema migrations

**Code references:**
- `src/zae_limiter/exceptions.py:260` - `VersionMismatchError`
- `src/zae_limiter/exceptions.py:290` - `IncompatibleSchemaError`
- `src/zae_limiter/version.py` - version checking logic

**Diagnostic/resolution commands:**
```bash
# Check compatibility
zae-limiter check --name <name> --region <region>

# Show version details
zae-limiter version --name <name> --region <region>

# Upgrade Lambda (non-breaking)
zae-limiter upgrade --name <name> --region <region>
```

**Reference existing content:**
- `docs/migrations.md` - full migration procedures

### Step 6: Stream Processing Lag

Document DynamoDB Streams latency issues.

**Migrate from `monitoring.md`:** "Increasing Iterator Age" section (lines 498-515)
- Existing content covers: symptoms, diagnostic steps, solutions (concurrency, errors, capacity)

**Content to cover (expand migrated content):**
- `IteratorAge` metric monitoring
- Shard splitting and scaling
- Lambda concurrency vs stream shards
- Backpressure from downstream systems
- Event ordering guarantees

**CloudWatch metrics to document:**
- `IteratorAge` - time since oldest record in stream
- Lambda `Duration`, `Errors`, `ConcurrentExecutions`
- DynamoDB `ReturnedRecordsCount`

**Diagnostic procedures:**
```bash
# Check stream status
aws dynamodb describe-table --table-name ZAEL-<name> \
  --query 'Table.StreamSpecification'

# Check Lambda event source mapping
aws lambda list-event-source-mappings \
  --function-name ZAEL-<name>-aggregator
```

### Step 7: Recovery Procedures

Document comprehensive recovery procedures.

**Content to cover:**
- **DynamoDB backup/restore:** PITR, on-demand backups, restore procedures
- **Migration rollback:** reversible vs forward-only migrations
- **Stack redeployment:** clean redeploy, preserving data
- **Data reconciliation:** fixing corrupted bucket states

**Reference existing content:**
- `docs/migrations.md` - Emergency Rollback Procedures section

**Add new content:**
- Step-by-step recovery playbooks
- Decision trees for choosing recovery approach
- Post-recovery verification steps

### Step 8: Quick Reference Section

Create a condensed reference for operators.

**Tables to include:**
1. CLI diagnostic commands quick reference
2. CloudWatch metric names and thresholds
3. Exception → likely cause → fix mapping
4. DynamoDB key patterns for manual queries

### Step 9: Update monitoring.md

Remove the Troubleshooting section from `monitoring.md` and replace with a link.

**Tasks:**
- [ ] Remove lines 466-559 (Troubleshooting section and Next Steps)
- [ ] Add new "Next Steps" section with link to troubleshooting guide:
  ```markdown
  ## Next Steps

  - [Troubleshooting Guide](troubleshooting.md) - Diagnose and resolve operational issues
  - [Performance Tuning](performance.md) - Capacity planning and optimization
  - [Deployment Guide](infra/deployment.md) - Infrastructure setup
  ```

This keeps `monitoring.md` focused on observability configuration (logging, metrics, dashboards, alerts).

### Step 10: Cross-linking and Integration

Ensure proper integration with existing documentation.

**Tasks:**
- [ ] Add "Troubleshooting" to `docs/index.md` navigation
- [ ] Link from `docs/guide/failure-modes.md` to relevant sections
- [ ] Link from `docs/migrations.md` to recovery procedures
- [ ] Add troubleshooting links to exception docstrings (optional)

## Testing the Documentation

After completion, validate the guide by:

1. **Technical accuracy:** Verify all commands work as documented
2. **Completeness:** Walk through each scenario with LocalStack
3. **Clarity:** Have someone unfamiliar with the system follow the guide

**Validation commands:**
```bash
# Start LocalStack
docker compose up -d

# Deploy test infrastructure
zae-limiter deploy --name test --endpoint-url http://localhost:4566 --region us-east-1

# Simulate failure scenarios and verify troubleshooting steps
# (Manual testing following the guide)
```

## File Changes Summary

| File | Action | Description |
|------|--------|-------------|
| `docs/troubleshooting.md` | Create | Main troubleshooting guide (consolidates all troubleshooting content) |
| `docs/monitoring.md` | Update | Remove Troubleshooting section, add link to troubleshooting.md |
| `docs/index.md` | Update | Add navigation link |
| `docs/guide/failure-modes.md` | Update | Add cross-reference |

## Acceptance Criteria

- [ ] All 6 requested scenarios are documented
- [ ] Each scenario includes: symptoms, diagnosis, causes, solutions, verification
- [ ] CLI commands are tested and accurate
- [ ] CloudWatch metrics and thresholds are specified
- [ ] Recovery procedures are actionable
- [ ] Cross-links to related documentation are in place
- [ ] Document follows existing docs style (markdown, code blocks, tables)
- [ ] `monitoring.md` Troubleshooting section removed and replaced with link
- [ ] No duplicate troubleshooting content across docs

## Notes

- The guide should be practical and action-oriented, not theoretical
- Include specific command examples with placeholder values
- Reference existing docs rather than duplicating content
- Consider adding a "Common Issues" FAQ section if space permits
- Coordinate with #45 and #46 when those are completed to add verification procedures
