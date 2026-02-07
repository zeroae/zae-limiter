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
| Missing `--policy-name-format` | `iam:CreatePolicy` denied for policies like `stress-target-acq` | Add `--policy-name-format "PowerUserPB-{}"` |
| Using `--no-iam` | Load stack fails: missing `AcquireOnlyPolicyArn`, `FullAccessPolicyArn` outputs | Use the three flags above instead |
| Using `--no-aggregator` alone | Works for limiter stack, but load stack still needs IAM policy ARNs | Use three flags + `--no-aggregator` only if aggregator is truly not needed |

### Deploying a limiter stack for benchmarks

```bash
# With aggregator (full stack)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter deploy \
  --name stress-target --region us-east-1 \
  --permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" \
  --role-name-format "PowerUserPB-{}" \
  --policy-name-format "PowerUserPB-{}"

# Without aggregator (benchmark-only, no Lambda needed)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter deploy \
  --name stress-target --region us-east-1 \
  --permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" \
  --role-name-format "PowerUserPB-{}" \
  --policy-name-format "PowerUserPB-{}" \
  --no-aggregator
```

### Deploying load test infrastructure

The load stack requires `AcquireOnlyPolicyArn` and `FullAccessPolicyArn` outputs from the limiter stack. The limiter stack **must** be deployed with IAM policies (not `--no-iam`).

```bash
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter load deploy \
  --name stress-target --region us-east-1 \
  --vpc-id vpc-09fa0359f30c6efe4 \
  --subnet-ids "subnet-0441a9342c2d605cf,subnet-0d607c058fe28230e" \
  -C examples/locust/
```

The load deploy inherits `PermissionBoundary` and `RoleNameFormat` from the limiter stack outputs.

### Running benchmarks

```bash
# Lambda mode (single invocation, simplest)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run zae-limiter load benchmark \
  --name stress-target --region us-east-1 \
  -f locustfiles/max_rps.py --users 10 --duration 60
```
