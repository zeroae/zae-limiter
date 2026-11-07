# AWS Testing Configuration

When developing new AWS e2e tests, deploy the stack manually via CLI first. This allows iterating on test code without the stack being deleted on each failure (pytest fixtures clean up on exit). Once the test passes, run it through pytest.

When running AWS tests (e.g., `pytest tests/e2e/test_aws.py --run-aws`), always use the following AWS profile:

```bash
AWS_PROFILE=zeroae-code/AWSPowerUserAccess
```

Example command:
```bash
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run pytest tests/e2e/test_aws.py --run-aws -v
```

If the SSO session has expired, run the login command directly:
```bash
aws sso login --profile zeroae-code/AWSPowerUserAccess
```

## Permission Boundary for IAM Resources

The PowerUserAccess profile lacks `iam:CreateRole` and `iam:CreatePolicy` by default. A conditional policy allows these actions **only when** the resource name matches `PowerUserPB-*` and a permission boundary is attached.

**Always pass all three flags** when deploying stacks that create IAM resources (roles or policies):

```bash
--permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" \
--role-name-format "PowerUserPB-{}" \
--policy-name-format "PowerUserPB-{}"
```

### Common mistakes

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Missing `--policy-name-format` | `iam:CreatePolicy` denied for policies like `load-test-acq` | Add `--policy-name-format "PowerUserPB-{}"` |
| Using `--no-iam` | Load stack fails: missing `AcquireOnlyPolicyArn`, `FullAccessPolicyArn` outputs | Use the three flags above instead |
| Using `--no-aggregator` alone | Works for limiter stack, but load stack still needs IAM policy ARNs | Use three flags + `--no-aggregator` only if aggregator is truly not needed |

### Deploying a limiter stack for benchmarks

```bash
# With aggregator (full stack)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter deploy \
  --name load-test --region us-east-1 \
  --permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" \
  --role-name-format "PowerUserPB-{}" \
  --policy-name-format "PowerUserPB-{}"

# Without aggregator (benchmark-only, no Lambda needed)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter deploy \
  --name load-test --region us-east-1 \
  --permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" \
  --role-name-format "PowerUserPB-{}" \
  --policy-name-format "PowerUserPB-{}" \
  --no-aggregator
```

### Deploying load test infrastructure

The load stack requires `AcquireOnlyPolicyArn` and `FullAccessPolicyArn` outputs from the limiter stack. The limiter stack **must** be deployed with IAM policies (not `--no-iam`).

```bash
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter loadtest deploy \
  --name load-test --region us-east-1 \
  --vpc-id vpc-09fa0359f30c6efe4 \
  --subnet-ids "subnet-0441a9342c2d605cf,subnet-0d607c058fe28230e" \
  -C examples/locust/
```

The loadtest deploy inherits `PermissionBoundary` and `RoleNameFormat` from the limiter stack outputs.

### Running tune and benchmarks

```bash
# Tune optimal per-worker user count (binary search)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter loadtest tune \
  --name load-test --region us-east-1 \
  -f locustfiles/max_rps.py --step-duration 30

# Single run, Lambda mode (simplest)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter loadtest run \
  --name load-test --region us-east-1 \
  -f locustfiles/max_rps.py --users 10 --duration 60
```

## AWS tests in CI (#189)

`.github/workflows/aws-e2e.yml` runs the suite through GitHub OIDC with a role that can write
only resources named `zci-*` (`.github/aws-e2e/`). Name everything an AWS test creates through
`tests/fixtures/names.py` — the `unique_name` fixtures or `stack_name()`, `iam_name()`,
`IAM_NAME_FORMAT`, `PERMISSION_BOUNDARY` — never a hardcoded `PowerUserPB-` or
`PowerUserAccess`. The defaults keep the SSO profile above working; CI overrides them with
`ZAEL_E2E_STACK_PREFIX`, `ZAEL_E2E_IAM_NAME_PREFIX` and `ZAEL_E2E_PERMISSION_BOUNDARY`. A
hardcoded name passes locally and fails in CI with `AccessDenied`.

A new AWS action a test needs goes into the role's policies in
`.github/aws-e2e/cfn_template.yaml`, scoped to `zci-*`, and the stack is redeployed by an
administrator (README there).
