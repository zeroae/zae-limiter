"""
Usage snapshot formatters.

This module provides formatters for rendering UsageSnapshot data
in different formats (table, ASCII charts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..models import UsageSnapshot


class BaseFormatter(Protocol):
    """Protocol for usage snapshot formatters."""

    def format(self, snapshots: list[UsageSnapshot]) -> str:
        """
        Format snapshots into output string.

        Args:
            snapshots: List of usage snapshots

        Returns:
            Formatted string representation
        """
        ...


class TableFormatter:
    """Format snapshots as a table (existing CLI format)."""

    def format(self, snapshots: list[UsageSnapshot]) -> str:
        """
        Generate table with columns: Window Start, Type, Resource, Entity, Events, Counters.

        Args:
            snapshots: List of usage snapshots

        Returns:
            Table-formatted string
        """
        if not snapshots:
            return ""

        lines: list[str] = []
        lines.append("")
        lines.append("Usage Snapshots")
        lines.append("=" * 100)
        lines.append("")
        lines.append(
            f"{'Window Start':<22} {'Type':<8} {'Resource':<16} "
            f"{'Entity':<20} {'Events':>8} Counters"
        )
        lines.append("-" * 100)

        for snap in snapshots:
            counters_str = ", ".join(f"{k}={v:,}" for k, v in sorted(snap.counters.items()))
            entity_display = snap.entity_id
            if len(entity_display) > 18:
                entity_display = entity_display[:15] + "..."
            resource_display = snap.resource
            if len(resource_display) > 14:
                resource_display = resource_display[:11] + "..."
            lines.append(
                f"{snap.window_start:<22} {snap.window_type:<8} {resource_display:<16} "
                f"{entity_display:<20} {snap.total_events:>8} {counters_str}"
            )

        return "\n".join(lines)


class PlotFormatter:
    """Format snapshots as ASCII charts (one chart per counter)."""

    # Default max data points to keep charts readable in standard terminals
    DEFAULT_MAX_POINTS = 60

    def __init__(self, height: int = 10, max_points: int | None = None) -> None:
        """
        Initialize the plot formatter.

        Args:
            height: Chart height in lines
            max_points: Maximum data points to display. If more snapshots are
                provided, they will be downsampled by averaging adjacent points.
                Default is 60 (fits ~80 char terminal with Y-axis labels).
                Set to 0 or None to disable downsampling.

        Raises:
            ImportError: If asciichartpy is not installed
        """
        try:
            import asciichartpy  # type: ignore[import-untyped]

            self._plot = asciichartpy.plot
        except ImportError as e:
            raise ImportError(
                "asciichartpy is required for plot format. "
                "Install with: pip install 'zae-limiter[plot]'"
            ) from e

        self._height = height
        self._max_points = max_points if max_points is not None else self.DEFAULT_MAX_POINTS

    def _downsample(self, values: list[float], target_points: int) -> list[float]:
        """
        Downsample values by averaging adjacent points.

        Args:
            values: Original values list
            target_points: Target number of points

        Returns:
            Downsampled values with approximately target_points entries
        """
        if len(values) <= target_points or target_points <= 0:
            return values

        # Calculate bucket size (how many original points per output point)
        bucket_size = len(values) / target_points
        result: list[float] = []

        for i in range(target_points):
            start = int(i * bucket_size)
            end = int((i + 1) * bucket_size)
            # Average the values in this bucket
            bucket_values = values[start:end]
            if bucket_values:
                result.append(sum(bucket_values) / len(bucket_values))

        return result

    def format(self, snapshots: list[UsageSnapshot]) -> str:
        """
        Generate ASCII charts - one per counter type.

        The snapshots are reversed to chronological order for plotting
        (oldest on left, newest on right).

        Args:
            snapshots: List of usage snapshots (reverse chronological)

        Returns:
            ASCII charts for each counter type
        """
        if not snapshots:
            return ""

        # Reverse for chronological order (oldest to newest, left to right)
        snapshots_chrono = list(reversed(snapshots))

        # Collect all counter names across snapshots
        counter_names: set[str] = set()
        for snap in snapshots_chrono:
            counter_names.update(snap.counters.keys())

        if not counter_names:
            return "No counter data to plot"

        # Extract entity and resource from first snapshot for context
        entity_id = snapshots_chrono[0].entity_id
        resource = snapshots_chrono[0].resource
        window_type = snapshots_chrono[0].window_type

        lines: list[str] = []

        # Check if downsampling is needed
        num_points = len(snapshots_chrono)
        downsampled = num_points > self._max_points > 0

        # Add header with context
        lines.append("")
        lines.append(f"Usage Plot: {resource} ({window_type})")
        lines.append(f"Entity: {entity_id}")
        if downsampled:
            lines.append(f"Note: {num_points} points downsampled to {self._max_points}")
        lines.append("=" * 80)

        # Generate charts for each counter
        charts: list[tuple[str, list[str]]] = []
        for counter_name in sorted(counter_names):
            values = [
                float(snap.counters.get(counter_name, 0)) for snap in snapshots_chrono
            ]

            # Downsample if needed
            if downsampled:
                values = self._downsample(values, self._max_points)

            # Calculate label width based on max value (including commas)
            max_val = max(values) if values else 0
            max_label = f"{max_val:,.0f}"
            label_width = len(max_label)

            # Build chart configuration with right-aligned labels
            config = {
                "height": self._height,
                "format": f"{{:>{label_width},.0f}} ",
            }

            chart = self._plot(values, config)
            chart_lines = chart.split("\n")
            charts.append((counter_name.upper(), chart_lines))

        # Render charts side by side (2 per row)
        spacing = "    "  # Gap between charts
        for i in range(0, len(charts), 2):
            left_name, left_lines = charts[i]
            left_width = max(len(line) for line in left_lines) if left_lines else 0

            lines.append("")

            if i + 1 < len(charts):
                # Two charts side by side
                right_name, right_lines = charts[i + 1]
                right_width = max(len(line) for line in right_lines) if right_lines else 0
                lines.append(f"{left_name:<{left_width}}{spacing}{right_name}")
                lines.append(f"{'-' * left_width}{spacing}{'-' * right_width}")

                # Pad to same height
                max_height = max(len(left_lines), len(right_lines))
                left_lines = left_lines + [""] * (max_height - len(left_lines))
                right_lines = right_lines + [""] * (max_height - len(right_lines))

                for left_line, right_line in zip(left_lines, right_lines):
                    lines.append(f"{left_line:<{left_width}}{spacing}{right_line}")
            else:
                # Single chart (odd number of counters)
                lines.append(left_name)
                lines.append("-" * left_width)
                lines.extend(left_lines)

        # Show time range at the end
        lines.append("")
        lines.append(
            f"Time range: {snapshots_chrono[0].window_start} "
            f"to {snapshots_chrono[-1].window_start}"
        )
        lines.append(f"Data points: {len(snapshots_chrono)}")

        return "\n".join(lines)
