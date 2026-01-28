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

## Permission Boundary for Lambda Stacks

The PowerUserAccess profile lacks `iam:CreateRole`. For stacks with Lambda, use:

```bash
--permission-boundary "arn:aws:iam::aws:policy/PowerUserAccess" --role-name-format "PowerUserPB-{}"
```

Without aggregator (no IAM role needed): `--no-aggregator`

## Debugging Failed Tests

Use `--keep-stacks-on-failure` to preserve CloudFormation stacks after test failures:

```bash
AWS_PROFILE=zeroae-code/AWSPowerUserAccess uv run pytest tests/e2e/test_aws.py --run-aws -v --keep-stacks-on-failure
```

This allows inspecting the stack via AWS Console or CLI to diagnose issues. Stacks are automatically deleted on test success.
