"""``zae-limiter upgrade`` refreshes stale stack version tags on real AWS (#663).

A stack deployed by an older release carries that release's
``zae-limiter:version`` / ``schema-version`` / ``lambda-version`` tags, and
CloudFormation propagates them to the table and the Lambda functions.
``upgrade`` moves them to this build with a tag-only ``UpdateStack`` that must
keep every parameter (an omitted one reverts to its template default) and every
user tag. ``deploy`` onto an existing stack must not touch them at all.

Real AWS only: LocalStack cannot perform a tag-only stack update (see
``tests/fixtures/stack_tags.py``), and only AWS proves the propagation.

To run::

    AWS_PROFILE=zeroae-code/AWSPowerUserAccess \\
      uv run pytest tests/e2e/test_aws_upgrade_tags.py --run-aws -v

WARNING: creates a real stack (no aggregator, no alarms) and deletes it.
"""

import boto3
import pytest
from click.testing import CliRunner

from tests.fixtures.stack_tags import retag_stack, stack_description, stack_parameters, stack_tags
from zae_limiter import __version__
from zae_limiter.cli import cli
from zae_limiter.infra.stack_manager import (
    LAMBDA_VERSION_TAG_KEY,
    SCHEMA_VERSION_TAG_KEY,
    VERSION_TAG_KEY,
)
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version

pytestmark = [pytest.mark.aws, pytest.mark.e2e]

REGION = "us-east-1"
POWER_USER_FLAGS = [
    "--permission-boundary",
    "arn:aws:iam::aws:policy/PowerUserAccess",
    "--role-name-format",
    "PowerUserPB-{}",
    "--policy-name-format",
    "PowerUserPB-{}",
]

OLD = "0.1.0"
STALE_TAGS = {
    VERSION_TAG_KEY: OLD,
    SCHEMA_VERSION_TAG_KEY: OLD,
    LAMBDA_VERSION_TAG_KEY: OLD,
}
"""Version tags as a stack deployed by an older release carries them."""

USER_TAG = ("team", "limits")

PLACEHOLDER_CODE_MAX_BYTES = 10_000
"""The template's inline placeholder is a few hundred bytes; the real package is MBs."""


def _forget_lambda_stamp(stack: str) -> None:
    """Make the version record read as unknown, so ``upgrade`` does not stop early."""
    repo = SyncRepository(stack, REGION, None, _skip_deprecation_warning=True)
    try:
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=None)
    finally:
        repo.close()


def _resource_tags(stack: str) -> tuple[dict[str, str], dict[str, str]]:
    """Tags on the table and on the provisioner function."""
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    table_arn = dynamodb.describe_table(TableName=stack)["Table"]["TableArn"]
    table_tag_list = dynamodb.list_tags_of_resource(ResourceArn=table_arn)["Tags"]
    table = {t["Key"]: t["Value"] for t in table_tag_list}
    lam = boto3.client("lambda", region_name=REGION)
    function_arn = lam.get_function(FunctionName=f"{stack}-limits-provisioner")["Configuration"][
        "FunctionArn"
    ]
    function = lam.list_tags(Resource=function_arn)["Tags"]
    return table, function


def test_upgrade_refreshes_stale_version_tags(unique_name):
    stack = unique_name
    where = ["--name", stack, "--region", REGION]
    runner = CliRunner()
    cfn = boto3.client("cloudformation", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    try:
        result = runner.invoke(
            cli,
            [
                "deploy",
                *where,
                "--no-aggregator",
                "--no-alarms",
                "--wait",
                "--tag",
                "=".join(USER_TAG),
                *POWER_USER_FLAGS,
            ],
        )
        assert result.exit_code == 0, f"Deploy failed: {result.output}"
        parameters = stack_parameters(cfn, stack)
        assert parameters["EnableAggregator"] == "false"
        assert parameters["PermissionBoundary"].endswith(":policy/PowerUserAccess")

        # A stack as an older release leaves it: stale tags on the stack and,
        # through propagation, on the table.
        retag_stack(cfn, stack, STALE_TAGS)
        table_tags, _ = _resource_tags(stack)
        assert table_tags[VERSION_TAG_KEY] == OLD

        _forget_lambda_stamp(stack)
        result = runner.invoke(cli, ["upgrade", *where])
        assert result.exit_code == 0, f"Upgrade failed: {result.output}"
        assert "Stack tags updated" in result.output, result.output
        assert "Tag update failed" not in result.output, result.output

        # The update finished, and changed nothing but tags
        description = stack_description(cfn, stack)
        assert description["StackStatus"] == "UPDATE_COMPLETE"
        assert stack_parameters(cfn, stack) == parameters

        tags = stack_tags(cfn, stack)
        assert tags[VERSION_TAG_KEY] == __version__
        assert tags[SCHEMA_VERSION_TAG_KEY] == get_schema_version()
        assert tags[LAMBDA_VERSION_TAG_KEY] == __version__
        assert tags[USER_TAG[0]] == USER_TAG[1]

        # CloudFormation propagated them to the resources
        table_tags, function_tags = _resource_tags(stack)
        assert table_tags[VERSION_TAG_KEY] == __version__
        assert table_tags[USER_TAG[0]] == USER_TAG[1]
        assert function_tags[VERSION_TAG_KEY] == __version__

        # The tag update did not put the template's placeholder code back
        code_size = lam.get_function(FunctionName=f"{stack}-limits-provisioner")["Configuration"][
            "CodeSize"
        ]
        assert code_size > PLACEHOLDER_CODE_MAX_BYTES

        # A stack auto-updated by open(): current version record, stale tags.
        # upgrade reports "already up to date" and still refreshes the tags,
        # without pushing code.
        retag_stack(cfn, stack, STALE_TAGS)
        code_sha = lam.get_function(FunctionName=f"{stack}-limits-provisioner")["Configuration"][
            "CodeSha256"
        ]
        result = runner.invoke(cli, ["upgrade", *where])
        assert result.exit_code == 0, f"Upgrade failed: {result.output}"
        assert "already up to date" in result.output, result.output
        assert "Stack tags updated" in result.output, result.output
        assert stack_description(cfn, stack)["StackStatus"] == "UPDATE_COMPLETE"
        assert stack_parameters(cfn, stack) == parameters
        tags = stack_tags(cfn, stack)
        assert tags[VERSION_TAG_KEY] == __version__
        assert tags[LAMBDA_VERSION_TAG_KEY] == __version__
        assert tags[USER_TAG[0]] == USER_TAG[1]
        table_tags, _ = _resource_tags(stack)
        assert table_tags[VERSION_TAG_KEY] == __version__
        assert (
            lam.get_function(FunctionName=f"{stack}-limits-provisioner")["Configuration"][
                "CodeSha256"
            ]
            == code_sha
        )

        # deploy onto the existing stack leaves version tags alone, even when
        # they are not this build's (an older client must not rewrite them)
        retag_stack(cfn, stack, {VERSION_TAG_KEY: "9.9.9"})
        last_updated = stack_description(cfn, stack)["LastUpdatedTime"]
        result = runner.invoke(
            cli, ["deploy", *where, "--no-aggregator", "--no-alarms", "--wait", *POWER_USER_FLAGS]
        )
        assert result.exit_code == 0, f"Re-deploy failed: {result.output}"
        assert stack_tags(cfn, stack)[VERSION_TAG_KEY] == "9.9.9"
        assert stack_description(cfn, stack)["LastUpdatedTime"] == last_updated
    finally:
        runner.invoke(cli, ["delete", *where, "--yes", "--wait"])
