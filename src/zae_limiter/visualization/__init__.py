"""
Visualization module for usage snapshot data.

Provides formatters for displaying usage snapshots in various formats:
- TABLE: Tabular text format (default)
- PLOT: ASCII line charts using asciichartpy

Example:
    from zae_limiter.visualization import UsageFormatter, format_usage_snapshots

    # Get formatted output
    output = format_usage_snapshots(snapshots, formatter=UsageFormatter.PLOT)
    print(output)
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import UsageSnapshot


class UsageFormatter(Enum):
    """Output format for usage snapshots."""

    TABLE = "table"
    PLOT = "plot"


def format_usage_snapshots(
    snapshots: list[UsageSnapshot],
    formatter: UsageFormatter = UsageFormatter.TABLE,
    **options: Any,
) -> str:
    """
    Format usage snapshots for display.

    Args:
        snapshots: List of usage snapshots (typically in reverse chronological order)
        formatter: Output format type (TABLE or PLOT)
        **options: Formatter-specific options:
            - height (int): Chart height in lines (PLOT only, default: 10)

    Returns:
        Formatted string ready for printing

    Raises:
        ImportError: If PLOT format requested but asciichartpy not installed.
            The error message includes installation instructions.

    Example:
        >>> from zae_limiter import RateLimiter
        >>> from zae_limiter.visualization import UsageFormatter, format_usage_snapshots
        >>>
        >>> limiter = RateLimiter(name="my-app", region="us-east-1")
        >>> snapshots, _ = await limiter.get_usage_snapshots(entity_id="user-1")
        >>> output = format_usage_snapshots(snapshots, formatter=UsageFormatter.PLOT)
        >>> print(output)
    """
    from .factory import get_formatter

    formatter_instance = get_formatter(formatter, **options)
    return formatter_instance.format(snapshots)


__all__ = ["UsageFormatter", "format_usage_snapshots"]
