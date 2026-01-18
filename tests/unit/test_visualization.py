"""Tests for the visualization module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from zae_limiter.models import UsageSnapshot
from zae_limiter.visualization import UsageFormatter, format_usage_snapshots
from zae_limiter.visualization.formatters import PlotFormatter, TableFormatter


@pytest.fixture
def sample_snapshots() -> list[UsageSnapshot]:
    """Create sample usage snapshots for testing."""
    return [
        UsageSnapshot(
            entity_id="test-entity",
            resource="gpt-4",
            window_start="2024-01-15T11:00:00Z",
            window_end="2024-01-15T11:59:59Z",
            window_type="hourly",
            counters={"tpm": 2000, "rpm": 10},
            total_events=10,
        ),
        UsageSnapshot(
            entity_id="test-entity",
            resource="gpt-4",
            window_start="2024-01-15T10:00:00Z",
            window_end="2024-01-15T10:59:59Z",
            window_type="hourly",
            counters={"tpm": 1000, "rpm": 5},
            total_events=5,
        ),
    ]


@pytest.fixture
def single_snapshot() -> list[UsageSnapshot]:
    """Create a single usage snapshot for testing."""
    return [
        UsageSnapshot(
            entity_id="test-entity",
            resource="gpt-4",
            window_start="2024-01-15T10:00:00Z",
            window_end="2024-01-15T10:59:59Z",
            window_type="hourly",
            counters={"tpm": 1000},
            total_events=5,
        ),
    ]


class TestTableFormatter:
    """Tests for TableFormatter."""

    def test_format_empty_snapshots(self) -> None:
        """Test formatting empty snapshot list."""
        formatter = TableFormatter()
        result = formatter.format([])
        assert result == ""

    def test_format_snapshots(self, sample_snapshots: list[UsageSnapshot]) -> None:
        """Test formatting snapshots as table."""
        formatter = TableFormatter()
        result = formatter.format(sample_snapshots)

        # Check table structure
        assert "Usage Snapshots" in result
        assert "Window Start" in result
        assert "Type" in result
        assert "Resource" in result
        assert "Entity" in result
        assert "Events" in result
        assert "Counters" in result
        assert "=" * 100 in result
        assert "-" * 100 in result

        # Check data
        assert "2024-01-15T11:00:00Z" in result
        assert "2024-01-15T10:00:00Z" in result
        assert "gpt-4" in result
        assert "test-entity" in result
        assert "hourly" in result
        assert "tpm=" in result
        assert "rpm=" in result

    def test_format_truncates_long_entity_id(self) -> None:
        """Test that long entity IDs are truncated."""
        snapshot = UsageSnapshot(
            entity_id="very-long-entity-id-that-exceeds-limit",
            resource="gpt-4",
            window_start="2024-01-15T10:00:00Z",
            window_end="2024-01-15T10:59:59Z",
            window_type="hourly",
            counters={"tpm": 1000},
            total_events=5,
        )
        formatter = TableFormatter()
        result = formatter.format([snapshot])

        assert "very-long-entit..." in result
        assert "very-long-entity-id-that-exceeds-limit" not in result

    def test_format_truncates_long_resource(self) -> None:
        """Test that long resource names are truncated."""
        snapshot = UsageSnapshot(
            entity_id="test-entity",
            resource="very-long-resource-name",
            window_start="2024-01-15T10:00:00Z",
            window_end="2024-01-15T10:59:59Z",
            window_type="hourly",
            counters={"tpm": 1000},
            total_events=5,
        )
        formatter = TableFormatter()
        result = formatter.format([snapshot])

        assert "very-long-r..." in result
        assert "very-long-resource-name" not in result


class TestPlotFormatter:
    """Tests for PlotFormatter."""

    def test_init_raises_import_error_without_asciichartpy(self) -> None:
        """Test that PlotFormatter raises ImportError if asciichartpy is not installed."""
        import builtins
        import sys

        original_import = builtins.__import__

        def mock_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "asciichartpy":
                raise ImportError("No module named 'asciichartpy'")
            return original_import(name, *args, **kwargs)

        # Remove asciichartpy from cache if present
        if "asciichartpy" in sys.modules:
            cached_module = sys.modules.pop("asciichartpy")
        else:
            cached_module = None

        try:
            with patch.object(builtins, "__import__", side_effect=mock_import):
                with pytest.raises(ImportError, match="asciichartpy is required"):
                    PlotFormatter()
        finally:
            # Restore the cached module
            if cached_module is not None:
                sys.modules["asciichartpy"] = cached_module

    def test_format_empty_snapshots(self) -> None:
        """Test formatting empty snapshot list."""
        pytest.importorskip("asciichartpy")
        formatter = PlotFormatter()
        result = formatter.format([])
        assert result == ""

    def test_format_snapshots_generates_charts(
        self, sample_snapshots: list[UsageSnapshot]
    ) -> None:
        """Test that format generates ASCII charts."""
        pytest.importorskip("asciichartpy")
        formatter = PlotFormatter(height=5)
        result = formatter.format(sample_snapshots)

        # Check header with entity/resource context
        assert "Usage Plot: gpt-4 (hourly)" in result
        assert "Entity: test-entity" in result
        assert "=" * 80 in result

        # Check counter labels
        assert "TPM" in result
        assert "RPM" in result

        # Check time range info
        assert "Time range:" in result
        assert "Data points: 2" in result

    def test_format_reverses_snapshots_for_chronological_order(
        self, sample_snapshots: list[UsageSnapshot]
    ) -> None:
        """Test that snapshots are reversed for chronological plotting."""
        pytest.importorskip("asciichartpy")
        formatter = PlotFormatter()
        result = formatter.format(sample_snapshots)

        # Time range should show oldest to newest
        # sample_snapshots has 11:00 first (newest), 10:00 second (oldest)
        # After reversal: 10:00 to 11:00
        assert "2024-01-15T10:00:00Z to 2024-01-15T11:00:00Z" in result

    def test_format_handles_missing_counters(self) -> None:
        """Test that missing counters are treated as 0."""
        pytest.importorskip("asciichartpy")
        snapshots = [
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start="2024-01-15T11:00:00Z",
                window_end="2024-01-15T11:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000, "rpm": 5},
                total_events=5,
            ),
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 500},  # Missing rpm
                total_events=3,
            ),
        ]
        formatter = PlotFormatter()
        result = formatter.format(snapshots)

        # Both counters should have charts
        assert "TPM" in result
        assert "RPM" in result

    def test_format_odd_number_of_counters(self) -> None:
        """Test that odd number of counters renders last one alone."""
        pytest.importorskip("asciichartpy")
        snapshots = [
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start="2024-01-15T11:00:00Z",
                window_end="2024-01-15T11:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000, "rpm": 10, "cost": 50},
                total_events=5,
            ),
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 500, "rpm": 5, "cost": 25},
                total_events=3,
            ),
        ]
        formatter = PlotFormatter(height=5)
        result = formatter.format(snapshots)

        # All three counters should have charts
        assert "COST" in result
        assert "RPM" in result
        assert "TPM" in result
        # First two should be side by side (COST and RPM alphabetically)
        # TPM should be on its own row
        lines = result.split("\n")
        # Find line with both COST and RPM (side by side header)
        side_by_side_header = [line for line in lines if "COST" in line and "RPM" in line]
        assert len(side_by_side_header) == 1, "COST and RPM should be on same line"

    def test_format_downsamples_large_datasets(self) -> None:
        """Test that large datasets are downsampled with a note."""
        pytest.importorskip("asciichartpy")
        # Create 100 snapshots (more than default max_points of 60)
        snapshots = [
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start=f"2024-01-15T{i:02d}:00:00Z",
                window_end=f"2024-01-15T{i:02d}:59:59Z",
                window_type="hourly",
                counters={"tpm": 500 + i * 10},
                total_events=5,
            )
            for i in range(100)
        ]
        # Reverse to simulate API order (newest first)
        snapshots = list(reversed(snapshots))

        formatter = PlotFormatter(height=5, max_points=60)
        result = formatter.format(snapshots)

        # Should show downsampling note
        assert "downsampled to 60" in result
        assert "100" in result  # Original point count mentioned
        # Should still show total data points
        assert "Data points: 100" in result

    def test_format_respects_max_points_zero(self) -> None:
        """Test that max_points=0 disables downsampling."""
        pytest.importorskip("asciichartpy")
        snapshots = [
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start=f"2024-01-15T{i:02d}:00:00Z",
                window_end=f"2024-01-15T{i:02d}:59:59Z",
                window_type="hourly",
                counters={"tpm": 500 + i * 10},
                total_events=5,
            )
            for i in range(100)
        ]
        snapshots = list(reversed(snapshots))

        # max_points=0 should disable downsampling
        formatter = PlotFormatter(height=5, max_points=0)
        result = formatter.format(snapshots)

        # Should NOT show downsampling note
        assert "downsampled" not in result

    def test_format_with_custom_height(
        self, single_snapshot: list[UsageSnapshot]
    ) -> None:
        """Test that custom height is respected."""
        pytest.importorskip("asciichartpy")
        formatter = PlotFormatter(height=3)
        result = formatter.format(single_snapshot)

        # Chart should be generated (exact line count depends on asciichartpy)
        assert "TPM" in result
        assert "Usage Plot:" in result

    def test_format_no_counters(self) -> None:
        """Test formatting snapshots with no counters."""
        pytest.importorskip("asciichartpy")
        snapshots = [
            UsageSnapshot(
                entity_id="test",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={},
                total_events=0,
            ),
        ]
        formatter = PlotFormatter()
        result = formatter.format(snapshots)
        assert result == "No counter data to plot"


class TestFormatUsageSnapshots:
    """Tests for format_usage_snapshots function."""

    def test_format_with_table(self, sample_snapshots: list[UsageSnapshot]) -> None:
        """Test format_usage_snapshots with TABLE formatter."""
        result = format_usage_snapshots(sample_snapshots, formatter=UsageFormatter.TABLE)
        assert "Usage Snapshots" in result
        assert "Window Start" in result

    def test_format_with_plot(self, sample_snapshots: list[UsageSnapshot]) -> None:
        """Test format_usage_snapshots with PLOT formatter."""
        pytest.importorskip("asciichartpy")
        result = format_usage_snapshots(sample_snapshots, formatter=UsageFormatter.PLOT)
        assert "Usage Plot: gpt-4 (hourly)" in result
        assert "Entity: test-entity" in result
        assert "TPM" in result
        assert "RPM" in result

    def test_format_default_is_table(self, sample_snapshots: list[UsageSnapshot]) -> None:
        """Test that default formatter is TABLE."""
        result = format_usage_snapshots(sample_snapshots)
        assert "Usage Snapshots" in result
        assert "Window Start" in result

    def test_format_plot_raises_import_error_without_dependency(
        self, sample_snapshots: list[UsageSnapshot]
    ) -> None:
        """Test that PLOT formatter raises ImportError if asciichartpy not installed."""
        with patch(
            "zae_limiter.visualization.factory.PlotFormatter",
            side_effect=ImportError(
                "asciichartpy is required for plot format. "
                "Install with: pip install 'zae-limiter[plot]'"
            ),
        ):
            with pytest.raises(ImportError, match="asciichartpy is required"):
                format_usage_snapshots(sample_snapshots, formatter=UsageFormatter.PLOT)

    def test_format_passes_options_to_formatter(
        self, sample_snapshots: list[UsageSnapshot]
    ) -> None:
        """Test that options are passed to formatter."""
        pytest.importorskip("asciichartpy")
        # Should not raise - just verify it accepts height parameter
        result = format_usage_snapshots(
            sample_snapshots,
            formatter=UsageFormatter.PLOT,
            height=5,
        )
        assert "Usage Plot:" in result
        assert "TPM" in result


class TestUsageFormatter:
    """Tests for UsageFormatter enum."""

    def test_enum_values(self) -> None:
        """Test UsageFormatter enum values."""
        assert UsageFormatter.TABLE.value == "table"
        assert UsageFormatter.PLOT.value == "plot"

    def test_enum_members(self) -> None:
        """Test UsageFormatter has expected members."""
        assert len(UsageFormatter) == 2
        assert UsageFormatter.TABLE in UsageFormatter
        assert UsageFormatter.PLOT in UsageFormatter


class TestGetFormatter:
    """Tests for get_formatter factory function."""

    def test_get_formatter_with_non_integer_height(
        self, sample_snapshots: list[UsageSnapshot]
    ) -> None:
        """Test that non-integer height falls back to default."""
        pytest.importorskip("asciichartpy")
        from zae_limiter.visualization.factory import get_formatter

        # Should not raise - falls back to default height of 10
        formatter = get_formatter(UsageFormatter.PLOT, height="invalid")  # type: ignore[arg-type]
        result = formatter.format(sample_snapshots)
        assert "Usage Plot:" in result
        assert "TPM" in result

    def test_get_formatter_unknown_type(self) -> None:
        """Test that unknown formatter type raises ValueError."""
        from zae_limiter.visualization.factory import get_formatter

        # Create a mock formatter type that's not TABLE or PLOT
        with pytest.raises(ValueError, match="Unknown formatter type"):
            get_formatter("invalid")  # type: ignore[arg-type]


class TestTableRenderer:
    """Tests for TableRenderer class."""

    def test_render_empty_headers(self) -> None:
        """Test rendering with empty headers returns empty string."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer()
        result = renderer.render([], [])
        assert result == ""

    def test_render_headers_only(self) -> None:
        """Test rendering headers with no data rows."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer()
        result = renderer.render(["Name", "Value"], [])

        # Check table structure
        assert "+------+-------+" in result
        assert "| Name | Value |" in result
        # Should have 4 lines: separator, header, separator, separator
        lines = result.strip().split("\n")
        assert len(lines) == 4

    def test_render_with_data(self) -> None:
        """Test rendering table with headers and data rows."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer()
        headers = ["Name", "Count", "Status"]
        rows = [
            ["item-1", "10", "active"],
            ["item-2", "5", "paused"],
        ]
        result = renderer.render(headers, rows)

        # Check structure
        assert "| Name   | Count | Status |" in result
        assert "| item-1 | 10    | active |" in result
        assert "| item-2 | 5     | paused |" in result
        # Separators
        assert "+--------+-------+--------+" in result

    def test_render_right_alignment(self) -> None:
        """Test rendering with right-aligned columns."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer(alignments=["l", "r", "r"])
        headers = ["Limit", "Total", "Average"]
        rows = [
            ["tpm", "1,000", "500.00"],
            ["rpm", "100", "50.00"],
        ]
        result = renderer.render(headers, rows)

        # Right-aligned columns should have padding on the left
        # Column widths: Limit=5, Total=5, Average=7
        assert "| tpm   | 1,000 |  500.00 |" in result
        assert "| rpm   |   100 |   50.00 |" in result

    def test_render_center_alignment(self) -> None:
        """Test rendering with center-aligned columns."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer(alignments=["c"])
        headers = ["Status"]
        rows = [["OK"], ["FAIL"]]
        result = renderer.render(headers, rows)

        # Center aligned should center the text
        assert "| Status |" in result
        assert "|   OK   |" in result
        assert "|  FAIL  |" in result

    def test_render_wide_cells(self) -> None:
        """Test that column widths adjust to content."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer()
        headers = ["ID", "Description"]
        rows = [
            ["1", "Short"],
            ["2", "A much longer description text"],
        ]
        result = renderer.render(headers, rows)

        # Column width should accommodate the longest content
        assert "A much longer description text" in result
        # Separator should be wide enough
        assert "+----+--------------------------------+" in result

    def test_render_mixed_alignments(self) -> None:
        """Test rendering with mixed alignment settings."""
        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer(alignments=["l", "c", "r"])
        headers = ["Left", "Center", "Right"]
        rows = [["a", "b", "c"]]
        result = renderer.render(headers, rows)

        # Each column should have its alignment
        assert "| a    |   b    |     c |" in result
