"""Version tracking and compatibility checking for zae-limiter infrastructure."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Current schema version - increment when schema changes
# 0.7.0: Composite bucket items (ADR-114) + ADD-based writes (ADR-115)
# 0.8.0: Composite limit config items (ADR-114 for configs)
# 0.9.0: Bucket PK migration (GHSA-76rv) - per-(entity, resource, shard) partition keys
# 0.10.0: Local Secondary Indexes (ADR-123) - 5 LSI slots, odd=ALL / even=KEYS_ONLY
CURRENT_SCHEMA_VERSION = "0.10.0"

# The first release whose readers understand a `reset_after` limit (ADR-139).
# A reader predating it ignores `l_{name}_rsa`, reads a quota with no reset, and
# fails (clients) or over-admits (the aggregator's shard clone). Writers refuse
# to store one until the stack's `lambda_version` reaches it, and raise the
# record's `client_min_version` to it when they do (#638).
MIN_READER_VERSION_FOR_RESET_AFTER = "0.15.0"


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
    requires_client_upgrade: bool = False
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
            requires_client_upgrade=True,
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


def _release(version: ParsedVersion) -> ParsedVersion:
    """The version with its prerelease tag dropped (``0.15.0-rc1`` -> ``0.15.0``)."""
    return ParsedVersion(version.major, version.minor, version.patch)


def reads_reset_after(lambda_version: str | None, own_version: str) -> bool:
    """Whether a stack stamped ``lambda_version`` reads ``reset_after`` limits (#638).

    True when the deployed Lambdas are at least
    :data:`MIN_READER_VERSION_FOR_RESET_AFTER`, compared on the release part
    only (a ``0.15.0`` release candidate counts, as it does for
    ``check_compatibility``'s Lambda comparison). Also true when the Lambdas
    are **exactly** the calling build: a build running this function
    understands ``reset_after`` by construction, and that is the only way a
    development build (``0.14.1.dev99+g…``, numbered below the release that
    introduces the feature) can prove it.

    A missing or unparseable ``lambda_version`` proves nothing, so it is False.
    """
    if lambda_version is None:
        return False
    if lambda_version == own_version:
        return True
    try:
        deployed = parse_version(lambda_version)
    except ValueError:
        return False
    return _release(deployed) >= parse_version(MIN_READER_VERSION_FOR_RESET_AFTER)


def ratcheted_client_min_version(stored: str | None, own_version: str) -> str | None:
    """The ``client_min_version`` a ``reset_after`` write must leave behind (#638 C).

    Returns the new value to store, or None when the stored minimum is already
    high enough. **Never lowers it**: a stored minimum above the target is kept.

    The target is :data:`MIN_READER_VERSION_FOR_RESET_AFTER`, capped at the
    writer's own version. The cap matters only for a development build numbered
    below the release (``0.14.1.dev99``): raising the minimum above the writer
    would make the writer's own next ``open()`` refuse to start. An unparseable
    own version is not a floor anyone can compare against, so nothing is raised.
    """
    try:
        own = parse_version(own_version)
    except ValueError:
        return None
    minimum = parse_version(MIN_READER_VERSION_FOR_RESET_AFTER)
    target = MIN_READER_VERSION_FOR_RESET_AFTER if own >= minimum else own_version
    try:
        current = parse_version(stored or "0.0.0")
    except ValueError:
        current = ParsedVersion(0, 0, 0)
    if parse_version(target) <= current:
        return None
    return target


def get_schema_version() -> str:
    """Get the current schema version."""
    return CURRENT_SCHEMA_VERSION
