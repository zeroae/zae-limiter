# Plan: Issue #37 - Create Operational Runbooks

**Issue**: [#37 - docs: create operational runbooks](https://github.com/zeroae/zae-limiter/issues/37)
**Target File**: `docs/operations.md`
**Estimated Effort**: 4-5 hours
**Dependencies**: #45 (benchmarks), #46 (E2E tests) - tracked in #68

## Overview

Create comprehensive operational runbooks for common procedures. The runbooks should be actionable, include verification steps, and reference existing documentation where appropriate.

## Requested Runbook Sections

1. Emergency rollback
2. Version upgrade process (0.x → 1.0.0)
3. Lambda failure recovery
4. Scaling limits dynamically
5. Monitoring aggregator health
6. Handling infrastructure failures

---

## Implementation Plan

### Phase 1: Document Structure & Prerequisites

**Task 1.1**: Create `docs/operations.md` with standard structure
- Title and purpose
- Prerequisites section (AWS CLI, permissions, CLI installed)
- Quick reference table linking to each runbook
- Conventions (warning boxes, verification steps)

**Task 1.2**: Cross-reference existing documentation
- Link to `docs/cli.md` for command reference
- Link to `docs/infra/deployment.md` for deployment details
- Link to `docs/migrations.md` for migration framework
- Link to `docs/guide/failure-modes.md` for failure handling

---

### Phase 2: Emergency Rollback Runbook

**Task 2.1**: Document CloudFormation rollback scenarios
- Stack update rollback (automatic on failure)
- Manual rollback via CloudFormation console
- When to use `zae-limiter delete` vs CloudFormation rollback

**Task 2.2**: Document Lambda rollback
- Rolling back Lambda code via `zae-limiter upgrade --lambda-only`
- Using AWS Lambda console to revert to previous version
- Considerations: Lambda versions are not versioned by this library (future enhancement)

**Task 2.3**: Document schema rollback considerations
- Current limitation: migrations are forward-only (no built-in rollback)
- Manual DynamoDB item cleanup if needed
- Point-in-time recovery (PITR) as last resort
- When to engage support vs self-recovery

**Verification Steps**:
- `zae-limiter status --name <stack>` shows healthy state
- `zae-limiter check --name <stack>` shows COMPATIBLE

---

### Phase 3: Version Upgrade Runbook (0.x → 1.0.0)

**Task 3.1**: Pre-upgrade checklist
- Verify current version: `zae-limiter version --name <stack>`
- Check compatibility: `zae-limiter check --name <stack>`
- Review release notes for breaking changes
- Backup considerations (PITR enabled?)
- Notify stakeholders / schedule maintenance window

**Task 3.2**: Upgrade execution steps
- Step-by-step CLI commands
- Expected output for each step
- How to interpret CompatibilityResult statuses:
  - COMPATIBLE: Safe to proceed
  - UPGRADE_AVAILABLE: Lambda update available
  - MIGRATION_REQUIRED: Schema migration needed
  - INCOMPATIBLE: Major version mismatch, manual intervention

**Task 3.3**: Post-upgrade verification
- Version verification commands
- Functional smoke tests
- Monitoring checks (no new errors)

**Task 3.4**: Rollback procedure if upgrade fails
- Reference emergency rollback section
- When upgrade can be retried vs requires manual fix

---

### Phase 4: Lambda Failure Recovery Runbook

**Task 4.1**: Identify Lambda failures
- CloudWatch alarm triggers (error rate, duration, DLQ)
- CloudWatch Logs Insights queries for error analysis
- DLQ message inspection

**Task 4.2**: Common failure scenarios and resolution
| Scenario | Symptoms | Resolution |
|----------|----------|------------|
| Timeout | Duration alarm, incomplete processing | Increase `--lambda-timeout` |
| Memory exhaustion | OOM in logs | Increase `--lambda-memory` |
| Permission denied | AccessDenied errors | Check IAM role, permission boundary |
| DynamoDB throttling | ProvisionedThroughputExceeded | Check table capacity, back off |
| Code bug | Consistent errors in logs | Deploy fix, check for bad data |

**Task 4.3**: DLQ processing
- How to inspect DLQ messages
- Reprocessing strategy (manual replay)
- Purging DLQ after resolution

**Task 4.4**: Redeployment
- `zae-limiter upgrade --lambda-only` for code updates
- Full `zae-limiter deploy` for configuration changes

---

### Phase 5: Scaling Limits Dynamically Runbook

**Task 5.1**: Understanding rate limit configuration
- Limits defined in code via `Limit` class
- Stored limits in DynamoDB (when `use_stored_limits=True`)
- Default vs stored limit precedence

**Task 5.2**: Adjusting limits at runtime
- Programmatic updates via `RateLimiter` API
- Direct DynamoDB updates (advanced, with caveats)
- No downtime required for limit changes

**Task 5.3**: Capacity planning for scale
- Reference `docs/performance.md` for RCU/WCU formulas
- DynamoDB on-demand vs provisioned considerations
- When to consider table partition strategies

**Task 5.4**: Monitoring after scaling changes
- Watch for throttling alarms
- Verify limit changes applied correctly

---

### Phase 6: Monitoring Aggregator Health Runbook

**Task 6.1**: Health indicators
- Lambda invocation success rate
- Stream iterator age (processing lag)
- DLQ depth (failed batches)
- Snapshot update frequency

**Task 6.2**: CloudWatch dashboard setup
- Key metrics to display
- Suggested widget configuration
- Sample dashboard JSON template

**Task 6.3**: CloudWatch Logs Insights queries
```
# Find errors in aggregator
fields @timestamp, @message
| filter @message like /ERROR/
| sort @timestamp desc
| limit 50

# Processing summary
fields @timestamp, processed, snapshots_updated, errors
| filter @message like /Processing complete/
| stats sum(processed) as total_records, sum(errors) as total_errors by bin(1h)
```

**Task 6.4**: Alert response procedures
- What each alarm means
- Escalation path
- When to disable aggregator temporarily

---

### Phase 7: Infrastructure Failure Handling Runbook

**Task 7.1**: CloudFormation stack failure recovery
- Reading CloudFormation events for root cause
- Common failures: IAM permissions, resource limits, naming conflicts
- Retry strategies
- Manual resource cleanup if stack is stuck

**Task 7.2**: DynamoDB unavailability
- FAIL_OPEN vs FAIL_CLOSED behavior (reference failure-modes.md)
- Monitoring for availability issues
- Failover considerations (multi-region is not yet supported)

**Task 7.3**: Network/connectivity issues
- LocalStack endpoint configuration errors
- VPC configuration (if applicable)
- Timeout tuning

**Task 7.4**: Point-in-time recovery (PITR)
- When PITR is enabled (--pitr-recovery-days)
- How to restore to a specific point in time
- Post-restoration verification steps

---

## File Structure

```markdown
# Operational Runbooks

## Prerequisites
## Quick Reference

## 1. Emergency Rollback
### 1.1 CloudFormation Rollback
### 1.2 Lambda Rollback
### 1.3 Schema Rollback Considerations

## 2. Version Upgrade (0.x → 1.0.0)
### 2.1 Pre-upgrade Checklist
### 2.2 Upgrade Execution
### 2.3 Post-upgrade Verification
### 2.4 Rollback if Failed

## 3. Lambda Failure Recovery
### 3.1 Identifying Failures
### 3.2 Common Scenarios
### 3.3 DLQ Processing
### 3.4 Redeployment

## 4. Scaling Limits Dynamically
### 4.1 Rate Limit Configuration
### 4.2 Runtime Adjustments
### 4.3 Capacity Planning
### 4.4 Post-scaling Monitoring

## 5. Monitoring Aggregator Health
### 5.1 Health Indicators
### 5.2 Dashboard Setup
### 5.3 Log Insights Queries
### 5.4 Alert Response

## 6. Infrastructure Failure Handling
### 6.1 CloudFormation Failures
### 6.2 DynamoDB Unavailability
### 6.3 Network Issues
### 6.4 Point-in-time Recovery
```

---

## Acceptance Criteria

- [ ] All six requested runbook sections documented
- [ ] Each runbook has clear prerequisites
- [ ] Step-by-step commands with expected output
- [ ] Verification steps after each procedure
- [ ] Cross-references to existing documentation
- [ ] CloudWatch Logs Insights queries included
- [ ] Warning callouts for destructive operations
- [ ] Tested against LocalStack where applicable

---

## Notes

- **Dependency on #45/#46**: Some verification steps and metrics thresholds depend on benchmark data from #45 and E2E test validation from #46. Placeholder values can be used initially.
- **Future enhancements**: Lambda versioning, automated rollback, multi-region support are not currently implemented and should be noted as limitations.
- **Style**: Follow existing docs style (see `docs/cli.md` for reference). Use tables for quick reference, code blocks for commands.
