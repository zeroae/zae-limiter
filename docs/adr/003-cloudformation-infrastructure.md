# ADR-003: CloudFormation Infrastructure Deployment

**Status:** Accepted
**Date:** 2026-01-10
**PR:** [#8](https://github.com/zeroae/zae-limiter/pull/8)
**Milestone:** v0.1.0

## Context

zae-limiter requires DynamoDB tables, Lambda functions, DynamoDB Streams, IAM roles, and CloudWatch resources. Users need a way to deploy and manage this infrastructure.

Options considered:
- Programmatic table creation via boto3
- CloudFormation templates
- CDK constructs
- Terraform modules

## Decision

Use CloudFormation as the primary infrastructure deployment mechanism, with a CLI for user convenience.

**Implementation:**
- Embedded CloudFormation template (`cfn_template.yaml`)
- CLI commands: `deploy`, `delete`, `status`, `cfn-template`
- Stack naming convention: `ZAEL-{identifier}` prefix
- Auto-detection of LocalStack for local development

## Consequences

**Positive:**
- Declarative infrastructure with drift detection
- Easy cleanup: delete entire stack removes all resources
- Native AWS integration (no external tools required)
- Template export for users who want customization
- Consistent naming across all AWS resources

**Negative:**
- CloudFormation deployment is slower than direct API calls
- Template complexity grows with features (alarms, DLQ, etc.)
- LocalStack CloudFormation support occasionally lags AWS

## Alternatives Considered

- **Programmatic creation only**: Rejected; no drift detection, harder cleanup, inconsistent state on partial failures
- **CDK-first**: Rejected; adds Node.js dependency, higher barrier for simple deployments
- **Terraform**: Rejected; external tool dependency, different ecosystem than target users
