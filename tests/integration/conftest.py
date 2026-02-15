"""Integration test fixtures for LocalStack."""

from tests.fixtures.names import unique_name, unique_name_class, unique_namespace  # noqa: F401
from tests.fixtures.repositories import localstack_limiter, test_repo  # noqa: F401
from tests.fixtures.stacks import (  # noqa: F401
    localstack_endpoint,
    minimal_stack_options,
    shared_aggregator_stack,
    shared_minimal_stack,
)
