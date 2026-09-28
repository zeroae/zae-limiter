"""Moto fixtures for unit tests with mocked AWS."""

import asyncio
from collections.abc import Awaitable
from unittest.mock import patch

import pytest
from moto import mock_aws

from zae_limiter import SyncRateLimiter, __version__
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version


@pytest.fixture
def aws_credentials(monkeypatch):
    """Mock AWS credentials for moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # Unset AWS_ENDPOINT_URL to ensure moto intercepts requests
    # (LocalStack tests use localstack_endpoint fixture which reads from env)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def mock_dynamodb(aws_credentials):
    """Mock DynamoDB for tests."""
    with mock_aws(), _patch_aiobotocore_response():
        yield


def _patch_aiobotocore_response():
    """
    Patch aiobotocore to work with moto's sync responses.

    Moto returns botocore.awsrequest.AWSResponse which has sync content,
    but aiobotocore expects async content. This patch wraps the response
    handling to convert sync content to async.

    See: https://github.com/aio-libs/aiobotocore/discussions/1300
    """
    from aiobotocore import endpoint

    original_convert = endpoint.convert_to_response_dict

    async def patched_convert(http_response, operation_model):
        # If content is not awaitable (moto's sync response), wrap it
        if hasattr(http_response, "_content") and not isinstance(http_response._content, Awaitable):
            # Create a future that returns the content
            fut: asyncio.Future[bytes] = asyncio.Future()
            fut.set_result(http_response.content)
            http_response._content = fut
        return await original_convert(http_response, operation_model)

    return patch.object(endpoint, "convert_to_response_dict", patched_convert)


@pytest.fixture
def sync_limiter(mock_dynamodb):
    """Create a SyncRateLimiter with mocked DynamoDB."""
    # Setup table + default namespace for SyncRepository.open()
    setup = SyncRepository(
        name="test-rate-limits", region="us-east-1", _skip_deprecation_warning=True
    )
    setup.create_table()
    setup._register_namespace("default")
    # A deployed stack's record, as `zae-limiter deploy` leaves it (#638).
    setup.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    setup.close()

    # moto's in-process backend is not thread-safe (#656): a concurrent
    # UpdateItem that adds attributes can raise "dictionary changed size
    # during iteration" inside moto's copy.deepcopy. Real DynamoDB has no
    # such race, so this is a test-fixture concern only.
    repo = SyncRepository.open(stack="test-rate-limits", parallel_mode="serial")
    limiter = SyncRateLimiter(repository=repo)
    with limiter:
        yield limiter


def lambda_zip() -> bytes:
    """A minimal Lambda deployment package, standing in for the real builds.

    ``build_lambda_package`` / ``build_provisioner_package`` pip-install into
    a temporary directory; a code push under moto needs only a valid zip.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("handler.py", "def handler(event, context):\n    return None\n")
    return buf.getvalue()


def create_stack_lambdas(stack: str, *suffixes: str) -> None:
    """Create moto Lambda functions ``{stack}-{suffix}`` (#644).

    Stands in for the functions a stack's template deployed, so a code push
    against a function the stack does *not* deploy meets the real
    ``ResourceNotFoundException`` rather than a mock of it. Call inside
    ``mock_dynamodb`` (which is ``mock_aws``).
    """
    import boto3

    iam = boto3.client("iam", region_name="us-east-1")
    role = iam.create_role(RoleName=f"{stack}-lambda-role", AssumeRolePolicyDocument="{}")["Role"][
        "Arn"
    ]
    client = boto3.client("lambda", region_name="us-east-1")
    for suffix in suffixes:
        client.create_function(
            FunctionName=f"{stack}-{suffix}",
            Runtime="python3.12",
            Role=role,
            Handler="handler.handler",
            Code={"ZipFile": lambda_zip()},
        )


def stack_lambda_versions(stack: str) -> dict[str, str | None]:
    """The ``zae-limiter:lambda-version`` tag of each of ``stack``'s functions.

    ``deploy_lambda_code`` / ``deploy_provisioner_code`` tag a function with
    the client version after pushing to it, so the tag is the evidence that
    code was pushed. Absent functions are not listed.
    """
    import boto3

    client = boto3.client("lambda", region_name="us-east-1")
    versions: dict[str, str | None] = {}
    for function in client.list_functions()["Functions"]:
        name = function["FunctionName"]
        if name.startswith(f"{stack}-"):
            tags = client.list_tags(Resource=function["FunctionArn"])["Tags"]
            versions[name.removeprefix(f"{stack}-")] = tags.get("zae-limiter:lambda-version")
    return versions
