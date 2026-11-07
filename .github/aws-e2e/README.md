# AWS e2e access for GitHub Actions

`cfn_template.yaml` gives `.github/workflows/aws-e2e.yml` short-lived AWS credentials through
GitHub OIDC (#189). No access keys are stored anywhere.

## What it creates

| Resource | Name | Purpose |
|----------|------|---------|
| OIDC provider | `token.actions.githubusercontent.com` | Lets IAM trust GitHub's tokens (one per account; skip with `CreateOIDCProvider=false`) |
| IAM role | `zael-github-e2e` | Assumed by the workflow. Trusts only `repo:zeroae/zae-limiter:environment:aws-e2e` |
| Managed policies | `zael-github-e2e-deploy`, `zael-github-e2e-iam` | The role's permissions |
| Managed policy | `zael-github-e2e-boundary` | Permission boundary every role the CI role creates must carry |

## What the role can do

Everything it can write is named `zci-*` in `us-east-1`: CloudFormation stacks, DynamoDB tables,
Lambda functions and their event source mappings, SQS queues, CloudWatch alarms, log groups
under `/aws/lambda/zci-*`, S3 buckets, and IAM roles and managed policies. Outside that prefix it
can only read: `DescribeStacks` / `ListStacks` and the tagging API (stack discovery), metrics,
traces, and AWS managed policies.

- **No escalation through the roles it creates.** `iam:CreateRole` and every role-policy change
  require the `zael-github-e2e-boundary` boundary, removing or changing a boundary is denied, and
  the boundary allows only data-plane access to `zci-*` tables, their logs, queues and buckets,
  and X-Ray writes. `iam:PassRole` is limited to Lambda.
- **It cannot change itself.** Its own role and policies are named `zael-github-e2e*`, outside the
  `zci-` prefix.
- **It cannot open a test resource to anyone else.** Resource policies (DynamoDB, Lambda
  permissions and function URLs, S3 bucket policies and ACLs, SQS permissions) are denied.

The tests name every resource through `tests/fixtures/names.py`; the workflow sets
`ZAEL_E2E_STACK_PREFIX=zci-`, `ZAEL_E2E_IAM_NAME_PREFIX=` (empty) and
`ZAEL_E2E_PERMISSION_BOUNDARY` to the boundary's ARN. Unset, the defaults match the
`AWSPowerUserAccess` SSO profile used for local runs.

## Deploy (once, by an account administrator)

The PowerUser SSO profile cannot create an OIDC provider or a role outside `PowerUserPB*`, so
this stack needs administrator credentials in the `zeroae-code` account (733153035800):

```bash
aws cloudformation deploy \
  --profile <admin-profile> --region us-east-1 \
  --stack-name zael-github-e2e \
  --template-file .github/aws-e2e/cfn_template.yaml \
  --capabilities CAPABILITY_NAMED_IAM

aws cloudformation describe-stacks --profile <admin-profile> --region us-east-1 \
  --stack-name zael-github-e2e --query 'Stacks[0].Outputs'
```

Pass `--parameter-overrides CreateOIDCProvider=false` if the account already has the GitHub
provider.

## GitHub side

1. Environment `aws-e2e` (Settings → Environments). Add required reviewers if runs should need
   approval; a deployment-branch rule would block the label-triggered PR runs, whose ref is
   `refs/pull/N/merge`.
2. Environment variables on `aws-e2e`, from the stack outputs:
   - `AWS_E2E_ROLE_ARN` = `RoleArn`
   - `AWS_E2E_PERMISSION_BOUNDARY` = `PermissionBoundaryArn`
3. Label `aws-e2e`.

```bash
gh api -X PUT repos/zeroae/zae-limiter/environments/aws-e2e
gh variable set AWS_E2E_ROLE_ARN --env aws-e2e \
  --body arn:aws:iam::733153035800:role/zael-github-e2e
gh variable set AWS_E2E_PERMISSION_BOUNDARY --env aws-e2e \
  --body arn:aws:iam::733153035800:policy/zael-github-e2e-boundary
gh label create aws-e2e --color FF9900 --description "Run the AWS e2e suite on this PR"
```

## Changing the permissions

When a test needs an action the role lacks, the run fails with `AccessDenied` naming it. Add the
action to `DeployPolicy` or `IamPolicy`, scoped to `zci-*` where the service allows it, check the
policies with `aws accessanalyzer validate-policy`, and redeploy with the command above.
