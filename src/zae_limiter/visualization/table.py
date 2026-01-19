"""
Generic table renderer with box-drawing borders.

This module provides a TableRenderer class for rendering tabular data
as formatted ASCII tables with customizable alignment.
"""

from __future__ import annotations


class TableRenderer:
    """Render data as a box-drawing table.

    Example output:
        +--------+-------+--------+
        | Name   | Count | Status |
        +--------+-------+--------+
        | item-1 |    10 | active |
        | item-2 |     5 | paused |
        +--------+-------+--------+
    """

    def __init__(self, alignments: list[str] | None = None) -> None:
        """Initialize the table renderer.

        Args:
            alignments: List of alignments per column ('l', 'r', or 'c').
                       Defaults to left-aligned for all columns.
        """
        self._alignments = alignments

    def render(self, headers: list[str], rows: list[list[str]]) -> str:
        """Render headers and rows as a formatted table.

        Args:
            headers: List of column header names
            rows: List of rows, each row is a list of cell values

        Returns:
            Formatted table string with box-drawing borders
        """
        if not headers:
            return ""

        # Calculate column widths (max of header and all cell values)
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(cell))

        # Default to left alignment
        alignments = self._alignments if self._alignments else ["l"] * len(headers)

        # Build separator line
        separator = "+-" + "-+-".join("-" * w for w in widths) + "-+"

        # Build header line
        header_cells = [h.ljust(widths[i]) for i, h in enumerate(headers)]
        header_line = "| " + " | ".join(header_cells) + " |"

        # Build output lines
        lines: list[str] = []
        lines.append(separator)
        lines.append(header_line)
        lines.append(separator)

        for row in rows:
            cells = []
            for i, cell in enumerate(row):
                w = widths[i] if i < len(widths) else len(cell)
                align = alignments[i] if i < len(alignments) else "l"
                if align == "r":
                    cells.append(cell.rjust(w))
                elif align == "c":
                    cells.append(cell.center(w))
                else:
                    cells.append(cell.ljust(w))
            lines.append("| " + " | ".join(cells) + " |")

        lines.append(separator)

        return "\n".join(lines)
