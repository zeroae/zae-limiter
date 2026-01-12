# Implementation Plan: Issue #83 - IAM Permission Boundaries and Customizable Role Naming

## Problem Summary

CloudFormation deployments fail in enterprise environments with restricted IAM permissions (e.g., `AWSPowerUserAccess`) because:
1. Users lack `iam:GetRole`/`iam:CreateRole` authorization on arbitrary role names
2. No support for IAM permission boundaries required by organizational policies
3. Role naming must follow specific patterns (e.g., `app-*`) mandated by IAM policies

## Proposed Solution

Add two new configuration options:
1. **Permission Boundary Support**: Optional ARN to attach to the Lambda execution role
2. **Customizable Role Naming**: Allow specifying a custom role name for compliance

## Implementation Steps

### Step 1: Update CloudFormation Template
**File**: `src/zae_limiter/infra/cfn_template.yaml`

Add two new parameters:

```yaml
Parameters:
  # ... existing parameters ...

  PermissionBoundaryArn:
    Type: String
    Default: ''
    Description: (Optional) ARN of the IAM permission boundary to attach to the Lambda role

  RoleName:
    Type: String
    Default: ''
    Description: >
      (Optional) Custom name for the Lambda execution role.
      If empty, uses default: {TableName}-aggregator-role.
      Supports compliance with organizational role naming policies.
```

Add condition:

```yaml
Conditions:
  # ... existing conditions ...
  HasPermissionBoundary: !Not [!Equals [!Ref PermissionBoundaryArn, '']]
  HasCustomRoleName: !Not [!Equals [!Ref RoleName, '']]
```

Update `AggregatorRole` resource (lines 222-269):

```yaml
AggregatorRole:
  Type: AWS::IAM::Role
  Condition: DeployAggregator
  Properties:
    RoleName: !If
      - HasCustomRoleName
      - !Ref RoleName
      - !Sub ${TableName}-aggregator-role
    PermissionsBoundary: !If
      - HasPermissionBoundary
      - !Ref PermissionBoundaryArn
      - !Ref AWS::NoValue
    # ... rest of properties unchanged ...
```

### Step 2: Update StackOptions Model
**File**: `src/zae_limiter/models.py`

Add new fields to `StackOptions` dataclass (around line 450):

```python
@dataclass(frozen=True)
class StackOptions:
    """
    Configuration options for CloudFormation stack creation and updates.
    ...
    Attributes:
        ...
        permission_boundary_arn: IAM permission boundary ARN to attach to Lambda role
        role_name: Custom name for the Lambda execution role
    """

    # ... existing fields ...
    permission_boundary_arn: str | None = None
    role_name: str | None = None

    def __post_init__(self) -> None:
        """Validate options."""
        # ... existing validation ...

        # Validate permission_boundary_arn format if provided
        if self.permission_boundary_arn:
            if not self.permission_boundary_arn.startswith("arn:"):
                raise ValueError("permission_boundary_arn must be a valid ARN")

        # Validate role_name format if provided (IAM role name constraints)
        if self.role_name:
            if len(self.role_name) > 64:
                raise ValueError("role_name must not exceed 64 characters")
            # IAM role names allow alphanumeric, plus, equals, comma, period, at, underscore, hyphen
            import re
            if not re.match(r'^[\w+=,.@-]+$', self.role_name):
                raise ValueError(
                    "role_name must contain only alphanumeric characters and +=,.@-_"
                )

    def to_parameters(self) -> dict[str, str]:
        """Convert to stack parameters dict for StackManager."""
        # ... existing code ...

        if self.permission_boundary_arn:
            params["permission_boundary_arn"] = self.permission_boundary_arn
        if self.role_name:
            params["role_name"] = self.role_name

        return params
```

### Step 3: Update Stack Manager Parameter Mapping
**File**: `src/zae_limiter/infra/stack_manager.py`

Add new parameter mappings in `_format_parameters()` (around line 123):

```python
param_mapping = {
    # ... existing mappings ...
    "permission_boundary_arn": "PermissionBoundaryArn",
    "role_name": "RoleName",
}
```

### Step 4: Update CLI
**File**: `src/zae_limiter/cli.py`

Add new CLI options to the `deploy` command (after line 122):

```python
@click.option(
    "--permission-boundary-arn",
    type=str,
    default=None,
    help="ARN of IAM permission boundary to attach to Lambda role (optional)",
)
@click.option(
    "--role-name",
    type=str,
    default=None,
    help="Custom name for Lambda execution role (optional, default: {table-name}-aggregator-role)",
)
```

Update the `deploy` function signature and `StackOptions` instantiation:

```python
def deploy(
    # ... existing params ...
    permission_boundary_arn: str | None,
    role_name: str | None,
) -> None:
    # ...
    stack_options = StackOptions(
        # ... existing options ...
        permission_boundary_arn=permission_boundary_arn,
        role_name=role_name,
    )
```

### Step 5: Add Tests
**File**: `tests/test_models.py` (add or extend)

```python
class TestStackOptions:
    def test_permission_boundary_arn_valid(self):
        opts = StackOptions(
            permission_boundary_arn="arn:aws:iam::123456789012:policy/MyBoundary"
        )
        params = opts.to_parameters()
        assert params["permission_boundary_arn"] == "arn:aws:iam::123456789012:policy/MyBoundary"

    def test_permission_boundary_arn_invalid(self):
        with pytest.raises(ValueError, match="must be a valid ARN"):
            StackOptions(permission_boundary_arn="not-an-arn")

    def test_role_name_valid(self):
        opts = StackOptions(role_name="my-app-aggregator-role")
        params = opts.to_parameters()
        assert params["role_name"] == "my-app-aggregator-role"

    def test_role_name_too_long(self):
        with pytest.raises(ValueError, match="must not exceed 64 characters"):
            StackOptions(role_name="a" * 65)

    def test_role_name_invalid_chars(self):
        with pytest.raises(ValueError, match="must contain only"):
            StackOptions(role_name="invalid/role/name")
```

**File**: `tests/test_stack_manager.py` (add or extend)

```python
async def test_create_stack_with_permission_boundary(mock_cfn_client):
    """Test that permission boundary is passed to CloudFormation."""
    manager = StackManager("test-table", "us-east-1")
    opts = StackOptions(
        permission_boundary_arn="arn:aws:iam::123456789012:policy/Boundary"
    )

    # Verify parameters include permission boundary
    params = manager._format_parameters(opts.to_parameters())
    boundary_param = next(
        (p for p in params if p["ParameterKey"] == "PermissionBoundaryArn"),
        None
    )
    assert boundary_param is not None
    assert boundary_param["ParameterValue"] == "arn:aws:iam::123456789012:policy/Boundary"

async def test_create_stack_with_custom_role_name(mock_cfn_client):
    """Test that custom role name is passed to CloudFormation."""
    manager = StackManager("test-table", "us-east-1")
    opts = StackOptions(role_name="my-custom-role")

    params = manager._format_parameters(opts.to_parameters())
    role_param = next(
        (p for p in params if p["ParameterKey"] == "RoleName"),
        None
    )
    assert role_param is not None
    assert role_param["ParameterValue"] == "my-custom-role"
```

### Step 6: Update Documentation
**File**: `CLAUDE.md`

Add to the CLI deploy command section:

```markdown
# Deploy with permission boundary (for restricted IAM environments)
zae-limiter deploy --table-name rate_limits --permission-boundary-arn arn:aws:iam::123456789012:policy/MyBoundary

# Deploy with custom role name (for organizational naming policies)
zae-limiter deploy --table-name rate_limits --role-name my-app-aggregator-role

# Combine both options
zae-limiter deploy --table-name rate_limits \
  --permission-boundary-arn arn:aws:iam::123456789012:policy/MyBoundary \
  --role-name my-app-aggregator-role
```

Update `StackOptions` docstring in the documentation.

## File Changes Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `src/zae_limiter/infra/cfn_template.yaml` | Modify | Add parameters, conditions, update AggregatorRole |
| `src/zae_limiter/models.py` | Modify | Add fields to StackOptions, validation, to_parameters |
| `src/zae_limiter/infra/stack_manager.py` | Modify | Add parameter mappings |
| `src/zae_limiter/cli.py` | Modify | Add CLI options for deploy command |
| `tests/test_models.py` | Modify | Add StackOptions tests |
| `tests/test_stack_manager.py` | Modify | Add stack creation tests |
| `CLAUDE.md` | Modify | Document new CLI options |

## Design Decisions

### 1. Simple `role_name` vs Template-based `role_name_format`

**Chosen**: Simple `role_name` parameter

**Rationale**:
- Simpler to implement and validate
- Most enterprises have predictable naming patterns they apply externally
- Avoids complexity of template variable substitution in CloudFormation
- Users who need dynamic names can construct them before passing to CLI/StackOptions

### 2. ARN Validation

**Chosen**: Basic prefix validation (`arn:`) rather than full ARN parsing

**Rationale**:
- IAM permission boundary ARNs vary across partitions (aws, aws-cn, aws-us-gov)
- Full regex validation is complex and may reject valid ARNs
- AWS will provide clear error if ARN is invalid

### 3. Default Behavior

**Chosen**: Empty string defaults in CloudFormation with conditional logic

**Rationale**:
- Maintains backward compatibility (no permission boundary, auto-generated role name)
- No changes required for existing deployments
- Clear opt-in for new features

## Testing Strategy

1. **Unit Tests**: Validate StackOptions validation logic
2. **Integration Tests**: Deploy stack with LocalStack using new options
3. **Manual Testing**: Test with real AWS account using `AWSPowerUserAccess` credentials

## Backward Compatibility

- **Fully backward compatible**: All new parameters are optional with sensible defaults
- **No migration required**: Existing stacks continue to work
- **Stack updates**: Existing stacks can be updated to add permission boundaries

## Security Considerations

- Permission boundaries add an extra layer of defense
- Custom role names don't affect security posture
- Both features enable deployment in more restricted environments (better security)
