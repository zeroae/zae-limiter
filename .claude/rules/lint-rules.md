# Lint Rule Changes

Never disable, ignore, or suppress lint rules (ruff, mypy, cfn-lint, etc.) without explicit user permission.

## When lint failures occur

1. **First**: Try to fix the underlying issue
2. **If it's a false positive**: Ask the user before adding ignore comments or config changes
3. **Explain**: Why you believe it's a false positive and what the workaround would be

## Examples of prohibited actions without permission

- Adding `# noqa`, `# type: ignore`, `# cfn-lint: ignore` comments
- Creating or modifying `.cfnlintrc`, `ruff.toml`, `mypy.ini` to ignore rules
- Adding `--ignore` flags to lint commands in CI

## What to do instead

Ask the user:
> "cfn-lint is failing with W3037 because it doesn't recognize `TransactWriteItems` as a valid DynamoDB action (false positive). Should I add a `.cfnlintrc` to ignore this rule, or would you prefer a different approach?"
