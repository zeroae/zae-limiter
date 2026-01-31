# AWS Partition Support

## Rule

Never hardcode `arn:aws:` in CloudFormation templates. Always use `${AWS::Partition}` pseudo-parameter.

## Why

Hardcoded `arn:aws:` breaks deployments in:
- AWS GovCloud (`aws-us-gov`)
- AWS China (`aws-cn`)
- Any future AWS partitions

## Correct Pattern

```yaml
# Wrong
- arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Correct
- !Sub "arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
```

## Enforcement

A pre-commit hook (`check-aws-partition`) automatically fails if `arn:aws:` is found in CloudFormation templates.
