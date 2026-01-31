# IAM Role Naming Design

**Issue:** #252 - IAM role names can exceed 64-character limit when stack name + role_name_format is too long

**Date:** 2026-01-30

## Problem

When using `role_name_format` with permission boundaries, the final IAM role names can exceed the AWS 64-character limit. The current design has two bugs:

1. **Incorrect embedding:** `-aggregator-role` is embedded in all role names, not just the Lambda role
2. **Insufficient validation:** Stack name and format template are validated separately, not combined

### Example of Current Bug

With `role_name_format="PowerUserPB-{}"` and `stack_name="my-app"`:

| Role | Current Name | Problem |
|------|--------------|---------|
| Aggregator | `PowerUserPB-my-app-aggregator-role` | OK but verbose |
| App | `PowerUserPB-my-app-aggregator-role-app` | "aggregator" wrong here |
| Readonly | `PowerUserPB-my-app-aggregator-role-readonly` | "aggregator" wrong here |

With suffix format `"{}-PowerUserPB"`:

| Role | Current Name | Problem |
|------|--------------|---------|
| App | `my-app-aggregator-role-PowerUserPB-app` | Suffix in wrong position |

## Solution

### API Change

Change `get_role_name()` to accept a component parameter:

```python
def get_role_name(self, stack_name: str, component: str) -> str | None:
    """
    Get the final role name for a given stack name and component.

    Args:
        stack_name: Stack name
        component: Role component (aggr, app, admin, read)

    Returns:
        Final role name, or None if role_name_format not set

    Raises:
        ValidationError: If resulting name exceeds 64 characters
    """
    if self.role_name_format is None:
        return None
    role_name = self.role_name_format.replace("{}", f"{stack_name}-{component}")
    if len(role_name) > 64:
        # Calculate max allowed stack name
        format_overhead = len(self.role_name_format) - 2  # subtract {}
        max_stack_len = 64 - format_overhead - 1 - len(component)  # -1 for dash
        raise ValidationError(
            "role_name",
            role_name,
            f"exceeds IAM 64-character limit by {len(role_name) - 64} characters. "
            f"Shorten stack name to max {max_stack_len} characters with this format."
        )
    return role_name
```

### Component Names

| Component | Length | Purpose | CFN Resource |
|-----------|--------|---------|--------------|
| `aggr` | 4 | Aggregator Lambda role | `AggregatorRole` |
| `app` | 3 | Application access | `AppRole` |
| `admin` | 5 | Admin/ops access | `AdminRole` |
| `read` | 4 | Read-only/monitoring | `ReadOnlyRole` |

**Invariant:** All component names must be ≤ 8 characters. This ensures users who choose a valid stack name today won't break on library upgrades (new stack name = new DynamoDB table = data loss).

### Default Role Names (Breaking Change)

Old defaults:
- `{stack}-aggregator-role`
- `{stack}-app-role`
- `{stack}-admin-role`
- `{stack}-readonly-role`

New defaults:
- `{stack}-aggr`
- `{stack}-app`
- `{stack}-admin`
- `{stack}-read`

### CloudFormation Changes

Replace single `RoleName` parameter with 4 parameters:

| Parameter | Used By |
|-----------|---------|
| `AggregatorRoleName` | `AggregatorRole` resource |
| `AppRoleName` | `AppRole` resource |
| `AdminRoleName` | `AdminRole` resource |
| `ReadOnlyRoleName` | `ReadOnlyRole` resource |

### Validation

| Location | Check | Value |
|----------|-------|-------|
| `naming.py` | Max stack name length | 55 chars |
| `StackOptions.__post_init__` | Max format template length | 55 chars (64 - 1 - 8) |
| `get_role_name()` | Final role name length | 64 chars with helpful error |

### Example Results

With `role_name_format="PowerUserPB-{}"` and `stack_name="my-app"`:

| Role | New Name | Length |
|------|----------|--------|
| Aggregator | `PowerUserPB-my-app-aggr` | 24 |
| App | `PowerUserPB-my-app-app` | 23 |
| Admin | `PowerUserPB-my-app-admin` | 25 |
| Read | `PowerUserPB-my-app-read` | 24 |

With `role_name_format="{}-PowerUserPB"` and `stack_name="my-app"`:

| Role | New Name | Length |
|------|----------|--------|
| Aggregator | `my-app-aggr-PowerUserPB` | 24 |
| App | `my-app-app-PowerUserPB` | 23 |
| Admin | `my-app-admin-PowerUserPB` | 25 |
| Read | `my-app-read-PowerUserPB` | 24 |

## Deliverables

1. **ADR for IAM role naming convention**
   - Document 8-char component limit invariant
   - Document naming pattern: `{format}.replace("{}", f"{stack}-{component}")`

2. **Update `models.py`**
   - Change `get_role_name(stack_name)` to `get_role_name(stack_name, component)`
   - Add validation with helpful error message
   - Update `__post_init__` format length check to 55
   - Update `to_parameters()` to generate 4 role name params
   - Define `ROLE_COMPONENTS = ["aggr", "app", "admin", "read"]` constant

3. **Update `naming.py`**
   - Change max stack name from 48 to 55 chars

4. **Update `cfn_template.yaml`**
   - Replace `RoleName` parameter with 4 parameters
   - Update each role resource to use its dedicated parameter
   - Update default names to drop `-role` suffix

5. **Unit tests (`test_models.py`)**
   - `get_role_name()` with prefix format for all components
   - `get_role_name()` with suffix format for all components
   - Length validation with helpful error message
   - Test that all `ROLE_COMPONENTS` are ≤ 8 chars (invariant test)

6. **E2E LocalStack tests**
   - Deploy with `role_name_format`
   - Verify all 4 roles follow the naming pattern
   - Test both prefix and suffix formats

7. **Update CLAUDE.md**
   - Document new naming constraints
   - Update maximum identifier length section

8. **Release notes**
   - Document breaking change for role naming
   - Migration notes for existing deployments

## Breaking Change Notes

Existing deployments using default role names will have roles renamed:
- `{stack}-aggregator-role` → `{stack}-aggr`
- `{stack}-app-role` → `{stack}-app`
- `{stack}-admin-role` → `{stack}-admin`
- `{stack}-readonly-role` → `{stack}-read`

CloudFormation will:
1. Create new roles with new names
2. Update Lambda to use new role
3. Delete old roles

Running Lambdas may briefly fail during the update. Users should plan for a maintenance window.
