"""Resource naming utilities.

This module provides centralized validation and normalization for resource names.
Names must satisfy the most restrictive cloud provider rules (currently AWS):
- Alphanumeric characters and hyphens only
- Must start with a letter
- Maximum 64 characters (IAM role name limit with suffix room)
"""

import re

from .exceptions import ValidationError

PREFIX = "ZAEL-"
"""Legacy prefix kept for backwards compatibility with pre-v0.7 stacks."""

# Name validation pattern (cloud-agnostic, satisfies AWS rules):
# - Alphanumeric and hyphens only
# - Must start with a letter
NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def validate_name(name: str) -> None:
    """
    Validate a resource name identifier.

    Args:
        name: The user-provided identifier

    Raises:
        ValidationError: If the name contains invalid characters
    """
    if not name:
        raise ValidationError(
            "name",
            name,
            "Name cannot be empty",
        )

    # Check for common invalid characters with helpful messages
    if "_" in name:
        raise ValidationError(
            "name",
            name,
            "Contains underscore. Use hyphens instead (e.g., 'rate-limits' not 'rate_limits')",
        )
    if "." in name:
        raise ValidationError(
            "name",
            name,
            "Contains period. Only alphanumeric characters and hyphens are allowed.",
        )
    if " " in name:
        raise ValidationError(
            "name",
            name,
            "Contains spaces. Use hyphens instead (e.g., 'my-app' not 'my app')",
        )

    # Full pattern validation
    if not NAME_PATTERN.match(name):
        raise ValidationError(
            "name",
            name,
            "Must start with a letter and contain only alphanumeric characters and hyphens.",
        )

    # Length validation (IAM role suffix is up to "-aggregator-role" = 16 chars)
    # IAM roles are limited to 64 chars total
    if len(name) > 48:
        raise ValidationError(
            "name",
            name,
            "Too long. Name exceeds 48 character limit (IAM role constraints).",
        )


def normalize_name(name: str) -> str:
    """
    Validate name and return as-is.

    For backwards compatibility, names with the legacy ``ZAEL-`` prefix
    are accepted and returned unchanged (the prefix is a valid name).

    Args:
        name: User-provided name

    Returns:
        Validated resource name (unchanged)

    Raises:
        ValidationError: If the name is invalid
    """
    validate_name(name)
    return name


# Aliases for internal AWS-specific code
validate_stack_name = validate_name
normalize_stack_name = normalize_name
