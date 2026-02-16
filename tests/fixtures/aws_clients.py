"""AWS client fixtures for E2E tests."""

import boto3
import pytest


def make_boto3_client(service: str, endpoint_url: str | None = None):
    """Create a boto3 client for a given service."""
    kwargs = {"region_name": "us-east-1"}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client(service, **kwargs)


@pytest.fixture
def cloudwatch_client(localstack_endpoint):
    """CloudWatch client for alarm verification."""
    return make_boto3_client("cloudwatch", localstack_endpoint)


@pytest.fixture
def sqs_client(localstack_endpoint):
    """SQS client for DLQ verification."""
    return make_boto3_client("sqs", localstack_endpoint)


@pytest.fixture
def lambda_client(localstack_endpoint):
    """Lambda client for function inspection."""
    return make_boto3_client("lambda", localstack_endpoint)


@pytest.fixture
def s3_client(localstack_endpoint):
    """S3 client for archive verification."""
    return make_boto3_client("s3", localstack_endpoint)


@pytest.fixture
def dynamodb_client(localstack_endpoint):
    """DynamoDB client for table inspection."""
    return make_boto3_client("dynamodb", localstack_endpoint)
