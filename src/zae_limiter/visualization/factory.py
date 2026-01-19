"""
Formatter factory for usage snapshot visualization.

Provides factory function to create appropriate formatter instances
based on the requested format type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import UsageFormatter
from .formatters import PlotFormatter, TableFormatter

if TYPE_CHECKING:
    from .formatters import BaseFormatter


def get_formatter(
    formatter_type: UsageFormatter,
    **options: Any,
) -> BaseFormatter:
    """
    Get formatter instance for the requested type.

    Args:
        formatter_type: Desired formatter (TABLE or PLOT)
        **options: Formatter-specific options:
            - height (int): Chart height for PLOT formatter (default: 10)
            - max_points (int): Max data points before downsampling (default: 60)

    Returns:
        Formatter instance matching the requested type

    Raises:
        ImportError: If PLOT format requested but asciichartpy not installed.
            Error message includes installation instructions.
        ValueError: If unknown formatter type requested
    """
    if formatter_type == UsageFormatter.TABLE:
        return TableFormatter()

    if formatter_type == UsageFormatter.PLOT:
        height = options.get("height", 10)
        if not isinstance(height, int):
            height = 10
        max_points = options.get("max_points")
        if max_points is not None and not isinstance(max_points, int):
            max_points = None
        return PlotFormatter(height=height, max_points=max_points)

    raise ValueError(f"Unknown formatter type: {formatter_type}")
