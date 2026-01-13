"""Resource naming utilities.

This module provides centralized validation and normalization for resource names.
Names must satisfy the most restrictive cloud provider rules (currently AWS):
- Alphanumeric characters and hyphens only
- Must start with a letter
- Maximum 38 characters (IAM role limit after prefix/suffix)
"""

import re

from .exceptions import ValidationError

PREFIX = "ZAEL-"

# Name validation pattern (cloud-agnostic, satisfies AWS rules):
# - Alphanumeric and hyphens only
# - Must start with a letter
NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def validate_name(name: str) -> None:
    """
    Validate a resource name identifier.

    Args:
        name: The user-provided identifier (without ZAEL- prefix)

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

    # Length validation (accounting for prefix and IAM role suffix)
    # IAM roles are limited to 64 chars, and we add "-aggregator-role" (16 chars)
    # So user identifier + prefix must be <= 48, but role_name_format may add more
    # Conservative limit: 38 chars for user identifier
    full_name = f"{PREFIX}{name}"
    if len(name) > 38:
        raise ValidationError(
            "name",
            name,
            "Too long. Name exceeds 38 character limit (IAM role constraints).",
        )
    if len(full_name) > 128:
        raise ValidationError(
            "name",
            name,
            f"Too long. Full name '{full_name}' exceeds 128 character limit.",
        )


def normalize_name(name: str) -> str:
    """
    Ensure name has the ZAEL- prefix and is valid.

    Args:
        name: User-provided name (with or without prefix)

    Returns:
        Full resource name with ZAEL- prefix

    Raises:
        ValidationError: If the name is invalid
    """
    # Strip prefix if already present for validation
    if name.startswith(PREFIX):
        identifier = name[len(PREFIX) :]
    else:
        identifier = name

    validate_name(identifier)
    return f"{PREFIX}{identifier}"


# Aliases for internal AWS-specific code
validate_stack_name = validate_name
normalize_stack_name = normalize_name
