# AWS Testing Configuration

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
