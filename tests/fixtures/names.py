"""Name generation fixtures for test isolation."""

import uuid

import pytest


@pytest.fixture
def unique_name():
    """Generate unique resource name for test isolation.

    Uses hyphens instead of underscores because AWS resource names
    must match pattern [a-zA-Z][-a-zA-Z0-9]*.
    """
    unique_id = uuid.uuid4().hex[:12]
    return f"test-{unique_id}"


@pytest.fixture(scope="class")
def unique_name_class():
    """Generate unique resource name for class-level test isolation."""
    unique_id = uuid.uuid4().hex[:12]
    return f"test-{unique_id}"


@pytest.fixture
def unique_namespace():
    """Generate unique namespace name for per-test data isolation."""
    unique_id = uuid.uuid4().hex[:8]
    return f"ns-{unique_id}"
