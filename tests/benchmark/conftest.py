"""Benchmark test fixtures.

Reuses fixtures from unit and integration for consistency.
"""

# Reuse moto fixtures for mocked benchmarks
from tests.unit.conftest import (
    _patch_aiobotocore_response,
    aws_credentials,
    mock_dynamodb,
    sync_limiter,
)

# Reuse LocalStack fixtures for realistic benchmarks
from tests.integration.conftest import (
    localstack_endpoint,
    minimal_stack_options,
    sync_localstack_limiter,
    unique_table_name,
)

__all__ = [
    "_patch_aiobotocore_response",
    "aws_credentials",
    "mock_dynamodb",
    "sync_limiter",
    "localstack_endpoint",
    "minimal_stack_options",
    "sync_localstack_limiter",
    "unique_table_name",
]
