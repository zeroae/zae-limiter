"""Name generation fixtures for test isolation.

Real-AWS tests name every resource they create through this module, so a CI
role scoped to one name prefix can run them (#189). Three environment
variables override the defaults, which match the ``AWSPowerUserAccess`` SSO
profile used for local runs (``.claude/rules/aws-testing.md``):

``ZAEL_E2E_STACK_PREFIX``
    Prepended to every generated stack name (default: none). CI sets ``zci-``.
``ZAEL_E2E_IAM_NAME_PREFIX``
    Prepended to every IAM role and managed policy name the tests create or
    ask a stack to create (default: ``PowerUserPB-``). CI sets it to empty, so
    IAM names start with the stack prefix too.
``ZAEL_E2E_PERMISSION_BOUNDARY``
    The boundary on every IAM role the tests create or ask a stack to create
    (default: the ``PowerUserAccess`` managed policy). CI sets the boundary
    from ``.github/aws-e2e/cfn_template.yaml``.
"""

import os
import uuid

import pytest

STACK_PREFIX = os.environ.get("ZAEL_E2E_STACK_PREFIX", "")
IAM_NAME_PREFIX = os.environ.get("ZAEL_E2E_IAM_NAME_PREFIX", "PowerUserPB-")
PERMISSION_BOUNDARY = os.environ.get(
    "ZAEL_E2E_PERMISSION_BOUNDARY", "arn:aws:iam::aws:policy/PowerUserAccess"
)
IAM_NAME_FORMAT = f"{IAM_NAME_PREFIX}{{}}"
"""``role_name_format`` / ``policy_name_format`` for stacks the tests deploy."""


def iam_name(name: str) -> str:
    """The IAM role or policy name the tests use for ``name``."""
    return f"{IAM_NAME_PREFIX}{name}"


def stack_name(name: str) -> str:
    """A stack name carrying the configured prefix."""
    return f"{STACK_PREFIX}{name}"


@pytest.fixture
def unique_name():
    """Generate unique resource name for test isolation.

    Uses hyphens instead of underscores because AWS resource names
    must match pattern [a-zA-Z][-a-zA-Z0-9]*.
    """
    unique_id = uuid.uuid4().hex[:12]
    return stack_name(f"test-{unique_id}")


@pytest.fixture(scope="class")
def unique_name_class():
    """Generate unique resource name for class-level test isolation."""
    unique_id = uuid.uuid4().hex[:12]
    return stack_name(f"test-{unique_id}")


@pytest.fixture
def unique_namespace():
    """Generate unique namespace name for per-test data isolation."""
    unique_id = uuid.uuid4().hex[:8]
    return f"ns-{unique_id}"
