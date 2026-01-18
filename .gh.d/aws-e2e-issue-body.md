## Summary

Add on-demand AWS E2E testing in GitHub Actions using OIDC authentication (no long-lived `ACCESS_KEY` secrets). Tests should run for approved PRs via a label trigger.

## Motivation

- Current E2E tests only run against LocalStack
- Real AWS testing catches issues that LocalStack doesn't emulate perfectly (IAM, CloudWatch, Lambda cold starts)
- OIDC authentication is the secure, modern approach - no secrets to rotate

## Implementation Plan

### Phase 1: AWS Infrastructure Setup

#### 1.1 Create GitHub OIDC Identity Provider

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: GitHub Actions OIDC Provider

Resources:
  GitHubOIDCProvider:
    Type: AWS::IAM::OIDCProvider
    Properties:
      Url: https://token.actions.githubusercontent.com
      ClientIdList:
        - sts.amazonaws.com
      ThumbprintList:
        - 6938fd4d98bab03faadb97b34396831e3780aea1
```

#### 1.2 Create IAM Role for GitHub Actions

- Trust policy restricts to `zeroae/zae-limiter` repo
- Resource-based restrictions to `ZAEL-e2e-*` resources only
- Compatible with Control Tower permission boundaries
- Least-privilege permissions for: CloudFormation, DynamoDB, Lambda, CloudWatch, SQS, IAM (for Lambda roles)

### Phase 2: GitHub Actions Workflow

Create `.github/workflows/aws-e2e.yml` with:

- **Triggers:**
  - `workflow_dispatch` for manual runs
  - `pull_request` with `labeled` event (triggers on `aws-e2e` label)

- **Authentication:**
  - Uses `aws-actions/configure-aws-credentials@v4` with OIDC
  - No long-lived secrets required
  - 1-hour session duration

- **Features:**
  - Concurrency control to prevent resource conflicts
  - Option to skip slow tests (monitoring, snapshots)
  - PR comment with test results
  - Automatic cleanup of orphaned stacks

### Phase 3: Security Controls

- [ ] Consider dedicated sandbox account in Control Tower for isolation
- [ ] Create `aws-e2e` GitHub environment with protection rules (optional approval gate)
- [ ] Fine-grained trust conditions in IAM role (branch/environment restrictions)

## Tasks

- [ ] Deploy GitHub OIDC Identity Provider to AWS
- [ ] Create IAM Role with appropriate permissions and trust policy
- [ ] Add `AWS_ACCOUNT_ID` to repository secrets
- [ ] Create `.github/workflows/aws-e2e.yml` workflow
- [ ] Create `aws-e2e` label in repository
- [ ] Update `tests/e2e/test_aws.py` to support configurable name prefix via environment variable
- [ ] (Optional) Create `aws-e2e` GitHub environment with approval rules
- [ ] Test end-to-end: manual dispatch and label-triggered runs
- [ ] Document the setup in CLAUDE.md

## Security Considerations

| Approach | Security | Recommendation |
|----------|----------|----------------|
| **OIDC + IAM Role** | ★★★★★ | **Recommended** |
| Long-lived ACCESS_KEY | ★★☆☆☆ | ❌ Avoid |

## How OIDC Works

```
┌─────────────────┐    1. Request OIDC token    ┌─────────────────┐
│  GitHub Actions │ ──────────────────────────► │  GitHub OIDC    │
│    Workflow     │ ◄────────────────────────── │    Provider     │
└────────┬────────┘    2. JWT token             └─────────────────┘
         │
         │ 3. AssumeRoleWithWebIdentity (JWT)
         ▼
┌─────────────────┐    4. Short-lived creds     ┌─────────────────┐
│    AWS STS      │ ──────────────────────────► │   IAM Role      │
│                 │                             │ (Trust GitHub)  │
└─────────────────┘                             └─────────────────┘
```

## References

- [GitHub OIDC with AWS](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
- [aws-actions/configure-aws-credentials](https://github.com/aws-actions/configure-aws-credentials)
