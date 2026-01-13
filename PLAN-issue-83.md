# Implementation Plan: Issue #83 - IAM Permission Boundaries and Customizable Role Naming

## Problem Summary

CloudFormation deployments fail in enterprise environments with restricted IAM permissions (e.g., `AWSPowerUserAccess`) because:
1. Users lack `iam:GetRole`/`iam:CreateRole` authorization on arbitrary role names
2. No support for IAM permission boundaries required by organizational policies
3. Role naming must follow specific patterns (e.g., `app-*`) mandated by IAM policies

## Proposed Solution

Add two new configuration options:
1. **Permission Boundary**: Policy name or ARN to attach to the Lambda execution role
2. **Role Name Format**: Template to wrap our internal role name with org-required prefix/suffix

## Final Design

### `permission_boundary`

Accepts either a policy name or full ARN. CloudFormation auto-constructs the ARN when given just a name.

| Input | Result |
|-------|--------|
| `PowerUserBoundary` | `arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/PowerUserBoundary` |
| `arn:aws:iam::123456789012:policy/MyBoundary` | Used as-is |

### `role_name_format`

Template using `{}` as placeholder for internal role name (`{table_name}-aggregator-role`).

| `role_name_format` | `table_name` | Result |
|-------------------|--------------|--------|
| (none) | `rate_limits` | `rate_limits-aggregator-role` |
| `pb-{}-PowerUser` | `rate_limits` | `pb-rate_limits-aggregator-role-PowerUser` |
| `app-{}` | `rate_limits` | `app-rate_limits-aggregator-role` |
| `{}-prod` | `rate_limits` | `rate_limits-aggregator-role-prod` |

Validation: if provided, must contain exactly one `{}`.

## Usage Examples

### CLI

```bash
# Simple policy name
zae-limiter deploy --table-name rate_limits \
  --permission-boundary PowerUserBoundary

# Full ARN
zae-limiter deploy --table-name rate_limits \
  --permission-boundary arn:aws:iam::123456789012:policy/MyBoundary

# Custom role name format
zae-limiter deploy --table-name rate_limits \
  --role-name-format "app-{}"

# Both options combined
zae-limiter deploy --table-name rate_limits \
  --permission-boundary PowerUserBoundary \
  --role-name-format "pb-{}-PowerUser"

# Full enterprise deployment
zae-limiter deploy --table-name rate_limits \
  --region us-east-1 \
  --permission-boundary PowerUserBoundary \
  --role-name-format "app-{}-prod" \
  --enable-alarms \
  --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:alerts
```

### StackOptions

```python
from zae_limiter import RateLimiter, StackOptions

# Simple policy name
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    stack_options=StackOptions(
        permission_boundary="PowerUserBoundary",
    ),
)

# Full ARN
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    stack_options=StackOptions(
        permission_boundary="arn:aws:iam::123456789012:policy/MyBoundary",
    ),
)

# With role name format
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    stack_options=StackOptions(
        permission_boundary="PowerUserBoundary",
        role_name_format="pb-{}-PowerUser",
    ),
)

# Full enterprise setup
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    stack_options=StackOptions(
        permission_boundary="PowerUserBoundary",
        role_name_format="app-{}-prod",
        enable_alarms=True,
        alarm_sns_topic="arn:aws:sns:us-east-1:123456789012:alerts",
    ),
)
```

## Implementation Steps

### Step 1: Update CloudFormation Template
**File**: `src/zae_limiter/infra/cfn_template.yaml`

Add new parameters:

```yaml
Parameters:
  # ... existing parameters ...

  PermissionBoundary:
    Type: String
    Default: ''
    Description: >
      (Optional) IAM permission boundary for the Lambda role.
      Accepts policy name or full ARN. If a name is provided,
      the ARN is constructed automatically.

  RoleNameFormat:
    Type: String
    Default: ''
    Description: >
      (Optional) Format template for the Lambda role name.
      Use {} as placeholder for the default role name.
      Example: "app-{}-prod" produces "app-mytable-aggregator-role-prod".
      If empty, uses default: {TableName}-aggregator-role.
```

Add conditions:

```yaml
Conditions:
  # ... existing conditions ...
  HasPermissionBoundary: !Not [!Equals [!Ref PermissionBoundary, '']]
  HasRoleNameFormat: !Not [!Equals [!Ref RoleNameFormat, '']]
  PermissionBoundaryIsArn: !And
    - !Condition HasPermissionBoundary
    - !Equals [!Select [0, !Split [':', !Ref PermissionBoundary]], 'arn']
```

Update `AggregatorRole` resource:

```yaml
AggregatorRole:
  Type: AWS::IAM::Role
  Condition: DeployAggregator
  Properties:
    RoleName: !If
      - HasRoleNameFormat
      - !Join
        - ''
        - !Split
          - '{}'
          - !Sub
            - '${Format}'
            - Format: !Ref RoleNameFormat
              DefaultRole: !Sub '${TableName}-aggregator-role'
      - !Sub ${TableName}-aggregator-role
    PermissionsBoundary: !If
      - HasPermissionBoundary
      - !If
        - PermissionBoundaryIsArn
        - !Ref PermissionBoundary
        - !Sub 'arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/${PermissionBoundary}'
      - !Ref AWS::NoValue
    AssumeRolePolicyDocument:
      # ... unchanged ...
```

**Note**: The `!Split`/`!Join` approach won't work directly. We need a different approach for the role name format substitution. Options:

**Option A**: Use a Lambda-backed custom resource to do the string substitution
**Option B**: Handle the substitution in Python before passing to CloudFormation
**Option C**: Use nested `!Sub` with a mapping

Recommended: **Option B** - Handle substitution in `stack_manager.py` before passing parameters to CloudFormation. This keeps the template simple and the logic testable in Python.

```yaml
# Simplified template - just accepts the final role name
RoleName:
  Type: String
  Default: ''
  Description: (Optional) Custom name for the Lambda execution role.
```

```yaml
AggregatorRole:
  Properties:
    RoleName: !If
      - HasCustomRoleName
      - !Ref RoleName
      - !Sub ${TableName}-aggregator-role
```

### Step 2: Update StackOptions Model
**File**: `src/zae_limiter/models.py`

```python
@dataclass(frozen=True)
class StackOptions:
    """
    Configuration options for CloudFormation stack creation and updates.

    Attributes:
        ...
        permission_boundary: IAM permission boundary (policy name or ARN)
        role_name_format: Format template for role name, {} = default role name
    """

    # ... existing fields ...
    permission_boundary: str | None = None
    role_name_format: str | None = None

    def __post_init__(self) -> None:
        """Validate options."""
        # ... existing validation ...

        # Validate role_name_format contains exactly one {}
        if self.role_name_format is not None:
            placeholder_count = self.role_name_format.count('{}')
            if placeholder_count != 1:
                raise ValueError(
                    f"role_name_format must contain exactly one '{{}}' placeholder, "
                    f"found {placeholder_count}"
                )
            # Validate resulting name won't exceed IAM limits (64 chars)
            # We can't fully validate without table_name, but we can check the format length
            format_len = len(self.role_name_format) - 2  # subtract {}
            if format_len > 40:  # leave room for table_name-aggregator-role
                raise ValueError(
                    "role_name_format template is too long, resulting role name "
                    "may exceed IAM 64 character limit"
                )

    def get_role_name(self, table_name: str) -> str | None:
        """
        Get the final role name for a given table name.

        Args:
            table_name: DynamoDB table name

        Returns:
            Final role name, or None to use CloudFormation default
        """
        if self.role_name_format is None:
            return None
        default_role = f"{table_name}-aggregator-role"
        return self.role_name_format.replace('{}', default_role)

    def to_parameters(self, table_name: str | None = None) -> dict[str, str]:
        """
        Convert to stack parameters dict for StackManager.

        Args:
            table_name: Table name for role_name_format substitution

        Returns:
            Dict with snake_case keys matching stack_manager parameter mapping.
        """
        # ... existing code ...

        if self.permission_boundary:
            params["permission_boundary"] = self.permission_boundary

        if self.role_name_format and table_name:
            params["role_name"] = self.get_role_name(table_name)

        return params
```

### Step 3: Update Stack Manager
**File**: `src/zae_limiter/infra/stack_manager.py`

Update parameter mapping:

```python
param_mapping = {
    # ... existing mappings ...
    "permission_boundary": "PermissionBoundary",
    "role_name": "RoleName",
}
```

Update `_format_parameters` to accept table_name for substitution:

```python
def _format_parameters(
    self,
    parameters: dict[str, str] | None,
    table_name: str | None = None,
) -> list[dict[str, str]]:
    # ... existing code, table_name used by StackOptions.to_parameters() ...
```

### Step 4: Update CLI
**File**: `src/zae_limiter/cli.py`

Add new options to deploy command:

```python
@click.option(
    "--permission-boundary",
    type=str,
    default=None,
    help=(
        "IAM permission boundary for Lambda role. "
        "Accepts policy name or full ARN."
    ),
)
@click.option(
    "--role-name-format",
    type=str,
    default=None,
    help=(
        "Format template for Lambda role name. "
        "Use {} as placeholder for default name. "
        "Example: 'app-{}' produces 'app-mytable-aggregator-role'."
    ),
)
def deploy(
    # ... existing params ...
    permission_boundary: str | None,
    role_name_format: str | None,
) -> None:
    # ...
    stack_options = StackOptions(
        # ... existing options ...
        permission_boundary=permission_boundary,
        role_name_format=role_name_format,
    )
```

### Step 5: Add Tests
**File**: `tests/test_models.py`

```python
class TestStackOptions:
    def test_permission_boundary_policy_name(self):
        opts = StackOptions(permission_boundary="MyBoundary")
        params = opts.to_parameters()
        assert params["permission_boundary"] == "MyBoundary"

    def test_permission_boundary_full_arn(self):
        arn = "arn:aws:iam::123456789012:policy/MyBoundary"
        opts = StackOptions(permission_boundary=arn)
        params = opts.to_parameters()
        assert params["permission_boundary"] == arn

    def test_role_name_format_valid(self):
        opts = StackOptions(role_name_format="app-{}")
        assert opts.get_role_name("mytable") == "app-mytable-aggregator-role"

    def test_role_name_format_prefix_suffix(self):
        opts = StackOptions(role_name_format="pb-{}-PowerUser")
        assert opts.get_role_name("mytable") == "pb-mytable-aggregator-role-PowerUser"

    def test_role_name_format_no_placeholder(self):
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(role_name_format="my-custom-role")

    def test_role_name_format_multiple_placeholders(self):
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(role_name_format="app-{}-{}-role")

    def test_role_name_format_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            StackOptions(role_name_format="a" * 50 + "-{}")
```

**File**: `tests/test_cli.py`

```python
def test_deploy_with_permission_boundary(runner, mock_stack_manager):
    result = runner.invoke(cli, [
        'deploy',
        '--table-name', 'test-table',
        '--permission-boundary', 'MyBoundary',
    ])
    assert result.exit_code == 0

def test_deploy_with_role_name_format(runner, mock_stack_manager):
    result = runner.invoke(cli, [
        'deploy',
        '--table-name', 'test-table',
        '--role-name-format', 'app-{}',
    ])
    assert result.exit_code == 0
```

### Step 6: Update Documentation
**File**: `CLAUDE.md`

Add to Infrastructure Deployment section:

```markdown
# Deploy with permission boundary (for restricted IAM environments)
zae-limiter deploy --table-name rate_limits \
  --permission-boundary MyBoundaryPolicy

# Deploy with custom role name format
zae-limiter deploy --table-name rate_limits \
  --role-name-format "app-{}"

# Enterprise deployment with both options
zae-limiter deploy --table-name rate_limits \
  --permission-boundary PowerUserBoundary \
  --role-name-format "pb-{}-PowerUser"
```

Update StackOptions documentation.

## File Changes Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `src/zae_limiter/infra/cfn_template.yaml` | Modify | Add PermissionBoundary, RoleName parameters and conditions |
| `src/zae_limiter/models.py` | Modify | Add fields, validation, get_role_name() method |
| `src/zae_limiter/infra/stack_manager.py` | Modify | Add parameter mappings |
| `src/zae_limiter/cli.py` | Modify | Add --permission-boundary, --role-name-format options |
| `tests/test_models.py` | Modify | Add StackOptions tests |
| `tests/test_cli.py` | Modify | Add CLI tests |
| `CLAUDE.md` | Modify | Document new options |

## Design Decisions

### 1. Permission Boundary: Policy Name or ARN

**Decision**: Accept either, CloudFormation constructs ARN from name

**Rationale**:
- Simpler for users (no need to know account ID)
- Handles all AWS partitions automatically (aws, aws-cn, aws-us-gov)
- Still allows full ARN for cross-account scenarios

### 2. Role Name Format: `{}` Placeholder

**Decision**: Use `{}` placeholder for the default role name

**Rationale**:
- Bash-safe (no escaping needed)
- Simple and clean syntax
- Enforces traceability (must include our internal name)
- Prevents arbitrary role names while allowing prefix/suffix
- Single placeholder keeps it simple

### 3. String Substitution: Python vs CloudFormation

**Decision**: Handle `{}` substitution in Python (StackOptions.get_role_name)

**Rationale**:
- CloudFormation doesn't have a clean way to do arbitrary string substitution
- Keeps the CFN template simple
- Logic is testable in Python
- Validation happens before deployment

## Backward Compatibility

- **Fully backward compatible**: All new parameters are optional with sensible defaults
- **No migration required**: Existing stacks continue to work
- **Stack updates**: Existing stacks can be updated to add permission boundaries

## Security Considerations

- Permission boundaries add an extra layer of defense
- Custom role names don't affect security posture
- Both features enable deployment in more restricted environments (better security)
