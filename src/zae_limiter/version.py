"""Version tracking and compatibility checking for zae-limiter infrastructure."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Current schema version - increment when schema changes
CURRENT_SCHEMA_VERSION = "1.1.0"


@dataclass(frozen=True, order=False)
class ParsedVersion:
    """Parsed semantic version components."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            return f"{base}-{self.prerelease}"
        return base

    def __lt__(self, other: ParsedVersion) -> bool:
        # Compare major.minor.patch first
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        # Prerelease versions are less than release versions
        if self.prerelease and not other.prerelease:
            return True
        if not self.prerelease and other.prerelease:
            return False
        # Both have prerelease, compare lexically
        return (self.prerelease or "") < (other.prerelease or "")

    def __le__(self, other: ParsedVersion) -> bool:
        return self == other or self < other

    def __gt__(self, other: ParsedVersion) -> bool:
        return not self <= other

    def __ge__(self, other: ParsedVersion) -> bool:
        return not self < other


def parse_version(version_str: str) -> ParsedVersion:
    """
    Parse a semantic version string.

    Handles formats like:
    - "1.2.3"
    - "1.2.3-dev"
    - "1.2.3.dev123+gabcdef"
    - "0.1.0"

    Args:
        version_str: Version string to parse

    Returns:
        ParsedVersion tuple

    Raises:
        ValueError: If version string is invalid
    """
    # Remove leading 'v' if present
    if version_str.startswith("v"):
        version_str = version_str[1:]

    # Handle PEP 440 dev versions (e.g., "0.1.0.dev123+gabcdef")
    # Convert to semver-like format
    version_str = re.sub(r"\.dev\d+.*$", "-dev", version_str)

    # Match standard semver with optional prerelease
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", version_str)
    if not match:
        raise ValueError(f"Invalid version string: {version_str}")

    return ParsedVersion(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        prerelease=match.group(4),
    )


@dataclass
class InfrastructureVersion:
    """Version information for deployed infrastructure."""

    schema_version: str
    lambda_version: str | None
    template_version: str | None
    client_min_version: str

    @classmethod
    def from_record(cls, record: dict[str, str | None]) -> InfrastructureVersion:
        """Create from a version record dictionary."""
        return cls(
            schema_version=record.get("schema_version") or "1.0.0",
            lambda_version=record.get("lambda_version"),
            template_version=record.get("template_version"),
            client_min_version=record.get("client_min_version") or "0.0.0",
        )


@dataclass
class CompatibilityResult:
    """Result of a version compatibility check."""

    is_compatible: bool
    requires_schema_migration: bool = False
    requires_lambda_update: bool = False
    requires_template_update: bool = False
    message: str = ""


def check_compatibility(
    client_version: str,
    infra_version: InfrastructureVersion,
) -> CompatibilityResult:
    """
    Check compatibility between client and infrastructure versions.

    Rules:
    - Major version mismatch in schema: Always incompatible (requires migration)
    - Client version < client_min_version: Incompatible (client too old)
    - Lambda version < client version: Lambda update available
    - Patch version differences: Always compatible

    Args:
        client_version: The client library version (e.g., "1.2.3")
        infra_version: The infrastructure version information

    Returns:
        CompatibilityResult with compatibility status and details
    """
    try:
        client = parse_version(client_version)
    except ValueError:
        return CompatibilityResult(
            is_compatible=False,
            message=f"Invalid client version: {client_version}",
        )

    try:
        schema = parse_version(infra_version.schema_version)
    except ValueError:
        return CompatibilityResult(
            is_compatible=False,
            message=f"Invalid schema version: {infra_version.schema_version}",
        )

    try:
        min_version = parse_version(infra_version.client_min_version)
    except ValueError:
        min_version = ParsedVersion(0, 0, 0)

    # Check if client meets minimum version requirement
    if client < min_version:
        return CompatibilityResult(
            is_compatible=False,
            message=(
                f"Client version {client_version} is below minimum required "
                f"version {infra_version.client_min_version}. Please upgrade."
            ),
        )

    # Check schema compatibility (major version must match)
    if client.major != schema.major:
        return CompatibilityResult(
            is_compatible=False,
            requires_schema_migration=True,
            message=(
                f"Schema version mismatch: client major version {client.major} "
                f"!= schema major version {schema.major}. "
                "Schema migration required."
            ),
        )

    # Check if Lambda needs update
    requires_lambda_update = False
    if infra_version.lambda_version:
        try:
            lambda_v = parse_version(infra_version.lambda_version)
            # Lambda update needed if client is newer (ignoring prerelease for comparison)
            client_release = ParsedVersion(client.major, client.minor, client.patch)
            lambda_release = ParsedVersion(lambda_v.major, lambda_v.minor, lambda_v.patch)
            requires_lambda_update = lambda_release < client_release
        except ValueError:
            # If Lambda version is invalid, suggest update
            requires_lambda_update = True

    if requires_lambda_update:
        return CompatibilityResult(
            is_compatible=True,  # Can still work, but update available
            requires_lambda_update=True,
            message=(
                f"Lambda update available: {infra_version.lambda_version} -> {client_version}"
            ),
        )

    # Fully compatible
    return CompatibilityResult(
        is_compatible=True,
        message="Client and infrastructure versions are compatible.",
    )


def get_schema_version() -> str:
    """Get the current schema version."""
    return CURRENT_SCHEMA_VERSION
