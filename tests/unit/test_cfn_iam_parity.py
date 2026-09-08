"""Tests verifying IAM role policies match DynamoDB operations used in repository.py.

Parses the CloudFormation template and cross-references IAM actions against
actual boto3 DynamoDB method calls in the repository. Catches regressions
like issue #242 where dynamodb:BatchGetItem was missing from AppRole/AdminRole.
"""

import ast
from importlib.resources import files
from pathlib import Path

import yaml


def _load_cfn_template() -> dict:
    """Load and parse the CloudFormation template."""
    template_text = files("zae_limiter.infra").joinpath("cfn_template.yaml").read_text()
    # CloudFormation intrinsic functions (!Ref, !Sub, etc.) need custom loader
    loader = yaml.SafeLoader
    # Add constructors for CloudFormation tags
    for tag in (
        "Ref",
        "Sub",
        "GetAtt",
        "If",
        "Select",
        "Split",
        "Join",
        "Equals",
        "Not",
        "And",
        "Or",
        "Condition",
        "FindInMap",
    ):
        loader.add_constructor(
            f"!{tag}",
            lambda loader, node: loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_scalar(node),
        )
    return yaml.load(template_text, Loader=loader)


def _extract_policy_actions(template: dict, policy_key: str) -> set[str]:
    """Extract DynamoDB actions from a managed policy."""
    policy = template["Resources"][policy_key]
    policy_doc = policy["Properties"]["PolicyDocument"]
    actions: set[str] = set()
    for stmt in policy_doc["Statement"]:
        if stmt["Effect"] == "Allow":
            stmt_actions = stmt["Action"]
            if isinstance(stmt_actions, str):
                stmt_actions = [stmt_actions]
            actions.update(stmt_actions)
    return actions


def _snake_to_iam_action(method_name: str) -> str:
    """Convert boto3 snake_case method to dynamodb:PascalCase IAM action.

    Example: batch_get_item -> dynamodb:BatchGetItem
    """
    pascal = "".join(word.capitalize() for word in method_name.split("_"))
    return f"dynamodb:{pascal}"


# DynamoDB data-plane methods that map to IAM actions.
# Table management (create_table, delete_table, describe_table) and
# waiters (get_waiter) are excluded — they use different IAM actions
# and are not relevant to the App/Admin/ReadOnly role policies.
_DYNAMODB_DATA_METHODS = {
    "get_item",
    "put_item",
    "delete_item",
    "update_item",
    "query",
    "scan",
    "batch_get_item",
    "batch_write_item",
    "transact_write_items",
    "transact_get_items",
}


def _find_dynamodb_calls_in_repository() -> set[str]:
    """Parse repository.py AST to find DynamoDB client method calls.

    Looks for patterns like `client.get_item(...)` and `await client.query(...)`
    where the method name is a known DynamoDB data-plane operation.

    Returns IAM action names (e.g., dynamodb:GetItem).
    """
    repo_path = Path(__file__).parent.parent.parent / "src" / "zae_limiter" / "repository.py"
    tree = ast.parse(repo_path.read_text())

    found_actions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Handle `client.method(...)` and `await client.method(...)`
            if isinstance(func, ast.Attribute) and func.attr in _DYNAMODB_DATA_METHODS:
                found_actions.add(_snake_to_iam_action(func.attr))
    return found_actions


class TestIAMRoleParity:
    """Verify IAM role policies cover all DynamoDB operations used in code."""

    def setup_method(self) -> None:
        self.template = _load_cfn_template()
        self.repo_actions = _find_dynamodb_calls_in_repository()

    def test_repository_uses_dynamodb_operations(self) -> None:
        """Sanity check: repository.py should use at least some DynamoDB operations."""
        assert len(self.repo_actions) >= 5, (
            f"Expected at least 5 DynamoDB operations, found: {self.repo_actions}"
        )

    def test_full_access_policy_covers_all_repository_operations(self) -> None:
        """FullAccessPolicy should have every DynamoDB action used in repository.py.

        FullAccessPolicy is for apps and ops teams — it needs full CRUD access
        to all operations the library performs.
        """
        full_actions = _extract_policy_actions(self.template, "FullAccessPolicy")
        missing = self.repo_actions - full_actions

        assert not missing, (
            f"FullAccessPolicy is missing DynamoDB actions used in repository.py: {missing}. "
            f"Add them to the FullAccessPolicy in cfn_template.yaml."
        )

    def test_acquire_only_policy_covers_read_and_transact_operations(self) -> None:
        """AcquireOnlyPolicy should have read + transact operations for acquire() workflow.

        AcquireOnlyPolicy is for applications running acquire() only. It needs:
        - Read operations: GetItem, BatchGetItem, Query
        - Write: TransactWriteItems (atomic bucket updates)

        It intentionally excludes PutItem, DeleteItem, UpdateItem,
        BatchWriteItem (entity creation and config management).
        """
        acq_actions = _extract_policy_actions(self.template, "AcquireOnlyPolicy")
        required_acq_actions = {
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
            "dynamodb:Query",
            "dynamodb:TransactWriteItems",
        }
        missing = required_acq_actions - acq_actions

        assert not missing, (
            f"AcquireOnlyPolicy is missing required DynamoDB actions: {missing}. "
            f"Add them to the AcquireOnlyPolicy in cfn_template.yaml."
        )

    def test_readonly_role_has_only_read_operations(self) -> None:
        """ReadOnlyPolicy should have read-only DynamoDB actions.

        It should not have any write operations.
        """
        readonly_actions = _extract_policy_actions(self.template, "ReadOnlyPolicy")
        write_actions = {
            "dynamodb:PutItem",
            "dynamodb:DeleteItem",
            "dynamodb:UpdateItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:TransactWriteItems",
        }
        unexpected_writes = readonly_actions & write_actions

        assert not unexpected_writes, (
            f"ReadOnlyPolicy has write actions it shouldn't: {unexpected_writes}"
        )


def _extract_statement_actions(template: dict, policy_key: str, sid: str) -> set[str]:
    """Extract DynamoDB actions from a specific statement in a managed policy."""
    policy = template["Resources"][policy_key]
    for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
        if stmt.get("Sid") == sid:
            actions = stmt["Action"]
            if isinstance(actions, str):
                actions = [actions]
            return set(actions)
    raise ValueError(f"Statement {sid!r} not found in {policy_key}")


def _extract_leading_keys(template: dict, policy_key: str, sid: str) -> list[str]:
    """Extract LeadingKeys condition values from a specific statement."""
    policy = template["Resources"][policy_key]
    for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
        if stmt.get("Sid") == sid:
            return stmt["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
    raise ValueError(f"Statement {sid!r} not found in {policy_key}")


class TestNamespaceScopedPolicies:
    """Verify namespace-scoped IAM policies use tag-based access control."""

    def setup_method(self) -> None:
        self.template = _load_cfn_template()

    def test_all_six_managed_policies_present(self) -> None:
        """CFN template should contain 6 managed policies (3 table-level + 3 namespace)."""
        expected = {
            "AcquireOnlyPolicy",
            "FullAccessPolicy",
            "ReadOnlyPolicy",
            "NamespaceAcquirePolicy",
            "NamespaceFullAccessPolicy",
            "NamespaceReadOnlyPolicy",
        }
        managed_policies = {
            k
            for k, v in self.template["Resources"].items()
            if v["Type"] == "AWS::IAM::ManagedPolicy"
        }
        assert expected <= managed_policies, (
            f"Missing managed policies: {expected - managed_policies}"
        )

    def test_namespace_acquire_policy_actions(self) -> None:
        """ns-acq: UpdateItem on namespace prefix, read-only on shared prefix."""
        # Statement 1: namespace prefix — read + write
        ns_actions = _extract_statement_actions(
            self.template, "NamespaceAcquirePolicy", "NamespaceAcquireAccess"
        )
        assert ns_actions == {
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
            "dynamodb:Query",
            "dynamodb:UpdateItem",
        }

        # Statement 2: shared prefix — read-only
        shared_actions = _extract_statement_actions(
            self.template, "NamespaceAcquirePolicy", "SharedReadAccess"
        )
        assert shared_actions == {
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
            "dynamodb:Query",
        }

    def test_namespace_full_access_policy_actions(self) -> None:
        """ns-full: all CRUD on namespace + shared prefix (single statement)."""
        actions = _extract_statement_actions(
            self.template, "NamespaceFullAccessPolicy", "NamespaceFullAccess"
        )
        assert actions == {
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
            "dynamodb:Query",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        }

    def test_namespace_readonly_policy_actions(self) -> None:
        """ns-read: read-only on namespace + shared prefix (single statement)."""
        actions = _extract_statement_actions(
            self.template, "NamespaceReadOnlyPolicy", "NamespaceReadAccess"
        )
        assert actions == {
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
            "dynamodb:Query",
        }

    def test_namespace_acquire_leading_keys(self) -> None:
        """ns-acq: namespace prefix uses PrincipalTag, shared prefix uses _/*."""
        ns_keys = _extract_leading_keys(
            self.template, "NamespaceAcquirePolicy", "NamespaceAcquireAccess"
        )
        assert ns_keys == ["${aws:PrincipalTag/zael_namespace_id}/*"]

        shared_keys = _extract_leading_keys(
            self.template, "NamespaceAcquirePolicy", "SharedReadAccess"
        )
        assert shared_keys == ["_/*"]

    def test_namespace_full_access_leading_keys(self) -> None:
        """ns-full: both namespace and shared prefix in one statement."""
        keys = _extract_leading_keys(
            self.template, "NamespaceFullAccessPolicy", "NamespaceFullAccess"
        )
        assert keys == ["${aws:PrincipalTag/zael_namespace_id}/*", "_/*"]

    def test_namespace_readonly_leading_keys(self) -> None:
        """ns-read: both namespace and shared prefix in one statement."""
        keys = _extract_leading_keys(
            self.template, "NamespaceReadOnlyPolicy", "NamespaceReadAccess"
        )
        assert keys == ["${aws:PrincipalTag/zael_namespace_id}/*", "_/*"]

    def test_namespace_readonly_has_no_write_actions(self) -> None:
        """ns-read should not have any write operations."""
        all_actions = _extract_policy_actions(self.template, "NamespaceReadOnlyPolicy")
        write_actions = {
            "dynamodb:PutItem",
            "dynamodb:DeleteItem",
            "dynamodb:UpdateItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:TransactWriteItems",
        }
        unexpected_writes = all_actions & write_actions
        assert not unexpected_writes, (
            f"NamespaceReadOnlyPolicy has write actions: {unexpected_writes}"
        )

    def test_namespace_acquire_shared_has_no_write_actions(self) -> None:
        """ns-acq SharedReadAccess statement should be read-only (no UpdateItem on _/*)."""
        shared_actions = _extract_statement_actions(
            self.template, "NamespaceAcquirePolicy", "SharedReadAccess"
        )
        write_actions = {
            "dynamodb:PutItem",
            "dynamodb:DeleteItem",
            "dynamodb:UpdateItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:TransactWriteItems",
        }
        unexpected_writes = shared_actions & write_actions
        assert not unexpected_writes, (
            f"ns-acq SharedReadAccess has write actions: {unexpected_writes}"
        )

    def test_namespace_policies_use_deploy_iam_condition(self) -> None:
        """All namespace policies should use DeployIAM condition."""
        for policy_key in (
            "NamespaceAcquirePolicy",
            "NamespaceFullAccessPolicy",
            "NamespaceReadOnlyPolicy",
        ):
            condition = self.template["Resources"][policy_key].get("Condition")
            assert condition == "DeployIAM", (
                f"{policy_key} should use DeployIAM condition, got: {condition}"
            )

    def test_namespace_policy_outputs_present(self) -> None:
        """Stack outputs should include namespace policy ARNs."""
        outputs = self.template["Outputs"]
        for output_key in (
            "NamespaceAcquirePolicyArn",
            "NamespaceFullAccessPolicyArn",
            "NamespaceReadOnlyPolicyArn",
        ):
            assert output_key in outputs, f"Missing output: {output_key}"
            assert outputs[output_key].get("Condition") == "DeployIAM"


def _extract_role_inline_policy_actions(
    template: dict, role_key: str, policy_name: str
) -> set[str]:
    """Extract DynamoDB actions from a named inline policy on an IAM role.

    Roles carry their permissions under Properties.Policies (a list of
    {PolicyName, PolicyDocument}), unlike managed policies which expose
    PolicyDocument directly.
    """
    role = template["Resources"][role_key]
    for policy in role["Properties"]["Policies"]:
        if policy["PolicyName"] != policy_name:
            continue
        actions: set[str] = set()
        for stmt in policy["PolicyDocument"]["Statement"]:
            if stmt["Effect"] != "Allow":
                continue
            stmt_actions = stmt["Action"]
            if isinstance(stmt_actions, str):
                stmt_actions = [stmt_actions]
            actions.update(a for a in stmt_actions if a.startswith("dynamodb:"))
        return actions
    raise ValueError(f"Inline policy {policy_name!r} not found on {role_key}")


def _find_dynamodb_calls_in_package(package: str) -> set[str]:
    """Parse every module in a source package for DynamoDB client method calls.

    Same AST approach as _find_dynamodb_calls_in_repository, but across a
    whole package rather than a single module.

    Returns IAM action names (e.g., dynamodb:UpdateItem).
    """
    pkg_path = Path(__file__).parent.parent.parent / "src" / package
    found_actions: set[str] = set()
    for module in sorted(pkg_path.glob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _DYNAMODB_DATA_METHODS:
                    found_actions.add(_snake_to_iam_action(func.attr))
    return found_actions


class TestProvisionerRoleParity:
    """Verify the provisioner Lambda's role covers the operations it performs.

    The provisioner runs as its own Lambda with its own inline role policy,
    so it does not benefit from the App/Admin/FullAccess policy checks above.
    ADR-125's disable fan-out added the first update_item call in that
    package; without this guard a new DynamoDB verb reaches production as an
    AccessDeniedException at runtime.
    """

    def setup_method(self) -> None:
        self.template = _load_cfn_template()
        self.provisioner_actions = _find_dynamodb_calls_in_package("zae_limiter_provisioner")

    def test_provisioner_uses_dynamodb_operations(self) -> None:
        """Sanity check: the provisioner package should use DynamoDB operations."""
        assert len(self.provisioner_actions) >= 3, (
            f"Expected at least 3 DynamoDB operations, found: {self.provisioner_actions}"
        )

    def test_provisioner_role_covers_all_provisioner_operations(self) -> None:
        """ProvisionerRole must grant every DynamoDB action the package calls.

        The fan-out added by ADR-125 calls update_item to stamp bucket items.
        A missing action here fails at runtime *after* apply_changes has
        written config items but *before* the #PROVISIONER state record is
        updated, so managed state silently drifts from the table.
        """
        granted = _extract_role_inline_policy_actions(
            self.template, "ProvisionerRole", "DynamoDBAccess"
        )
        missing = self.provisioner_actions - granted

        assert not missing, (
            f"ProvisionerRole is missing DynamoDB actions used in "
            f"src/zae_limiter_provisioner/: {missing}. "
            f"Add them to the DynamoDBAccess policy on ProvisionerRole in cfn_template.yaml."
        )
