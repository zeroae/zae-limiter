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
