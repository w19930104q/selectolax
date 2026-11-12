"""Rectangular table extraction on top of the read-only Lexbor DOM API.

The engine only walks nodes and reads attributes/text, so the parsed document
is never modified. It is intentionally backend-agnostic: anything exposing the
small subset of the :class:`~selectolax.lexbor.LexborNode` interface used here
(``tag``, ``iter``, ``traverse``, ``attributes``, ``is_text_node`` and
``text_content``) can be passed in.

The HTML table model allows rows of different widths, cells that span several
grid positions through ``rowspan``/``colspan``, sectioning elements
(``thead``/``tbody``/``tfoot``) and nested tables. The public
:class:`ExtractedTable` representation resolves all of that into a rectangular
list of fixed-width rows, where every position holds either a cell's text or
an empty string placeholder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# Browsers cap table spans at 1000; mirror that so a single bogus attribute
# cannot force the grid to allocate millions of positions.
_MAX_SPAN = 1000

# The HTML parsing rules read the leading integer of a span attribute:
# "2" -> 2, "2.5" -> 2, "abc" -> parse error (treated as 1). A bare "0" has
# no positive leading integer here and falls through to the non-positive
# repair below.
_LEADING_INT = re.compile(r"^\s*(\d*[1-9]\d*)")

_SECTION_TAGS = ("thead", "tbody", "tfoot")
_CELL_TAGS = ("td", "th")


class _NodeLike(Protocol):
    """The subset of ``LexborNode`` the extraction engine relies on."""

    @property
    def tag(self) -> str | None: ...

    def iter(self, include_text: bool = ..., skip_empty: bool = ...) -> Any: ...

    def traverse(self, include_text: bool = ..., skip_empty: bool = ...) -> Any: ...

    @property
    def attributes(self) -> dict: ...

    @property
    def is_text_node(self) -> bool: ...

    @property
    def text_content(self) -> str | None: ...


@dataclass
class TableWarning:
    """A recoverable structural problem found while extracting one table.

    Warnings never stop extraction: the table is still returned with whatever
    rows could be built. Every position is reported relative to the table the
    warning belongs to, so the message can be traced back to the source markup.

    Attributes
    ----------
    code:
        Stable machine-readable identifier, one of ``"invalid-span"``,
        ``"span-overflow"``, ``"multiple-captions"``, ``"cell-outside-row"``
        or ``"extraction-error"``.
    message:
        Human-readable explanation, including the offending value when known.
    row:
        Zero-based grid row the problem refers to, or ``None`` when it is not
        tied to a row.
    column:
        Zero-based grid column the problem refers to, or ``None`` when it is
        not tied to a column.
    tag:
        Tag name of the offending element, or ``None`` for table-wide issues.
    attribute:
        Name of the offending attribute, or ``None`` for other issues.
    """

    code: str
    message: str
    row: int | None = None
    column: int | None = None
    tag: str | None = None
    attribute: str | None = None


@dataclass
class TableRow:
    """One materialized grid row.

    Attributes
    ----------
    section:
        The section the row came from: ``"thead"``, ``"tbody"`` or
        ``"tfoot"``. Rows written directly under ``<table>`` without a section
        element are reported as ``"tbody"``, matching the HTML parsing rules.
    cells:
        Rectangular list of cell texts. Positions covered by a
        ``rowspan``/``colspan`` and genuinely empty cells are always empty
        strings, never ``None``.
    """

    section: str
    cells: list[str] = field(default_factory=list)


@dataclass
class ExtractedTable:
    """One ``<table>`` rendered as CSV-compatible rectangular records.

    Attributes
    ----------
    index:
        Zero-based position of the table in document order, counting nested
        tables as well.
    caption:
        Text of the table's first ``<caption>``, with HTML whitespace
        collapsed, or an empty string when there is no caption.
    rows:
        All grid rows in ``thead`` -> ``tbody`` -> ``tfoot`` order (document
        order within each section), every row padded to the same width.
    header_rows:
        The subset of :attr:`rows` that came from ``<thead>``, as a
        convenience for callers building CSV headers. The same row objects are
        not duplicated: this list holds references to the matching entries of
        :attr:`rows`.
    warnings:
        Structural problems recovered from while building this table.
    """

    index: int
    caption: str = ""
    rows: list[TableRow] = field(default_factory=list)
    header_rows: list[TableRow] = field(default_factory=list)
    warnings: list[TableWarning] = field(default_factory=list)


def _normalize_text(text: str | None) -> str:
    """Collapse runs of HTML whitespace, matching rendered table text.

    ``str.split()`` treats every ASCII whitespace run (including newlines and
    tabs introduced purely by markup formatting) as one separator, which is
    how a browser lays cell text out. ``None`` and empty strings both become
    the fixed empty placeholder.
    """
    if not text:
        return ""
    return " ".join(text.split())


def _collect_text_excluding_nested_tables(node: _NodeLike) -> str:
    """Gather a cell's text while skipping any nested ``<table>`` subtree.

    A plain deep text walk would concatenate the inner table's cells into the
    outer cell, which is exactly the "nested table counted by the outer table"
    leak this module has to avoid. The walk therefore stops at every nested
    table boundary, at any depth; the inner table's content is still extracted
    on its own by :func:`extract_tables`.

    The traversal uses an explicit stack rather than recursion so a deeply
    nested cell cannot exhaust the C stack, mirroring the iterative walk used
    elsewhere in the package. Text-node order matches document order.
    """
    parts: list[str] = []
    stack: list[_NodeLike] = [node]

    while stack:
        current = stack.pop()
        if current.is_text_node:
            value = current.text_content
            if value:
                parts.append(value)
            continue
        if current is not node and current.tag == "table":
            # Nested table: do not descend into its subtree.
            continue

        children = list(current.iter(include_text=True))
        # Reverse push so children pop in document order (LIFO stack).
        stack.extend(reversed(children))
    return "".join(parts)


def _cell_text(cell: _NodeLike) -> str:
    return _normalize_text(_collect_text_excluding_nested_tables(cell))


def _parse_span(
    cell: _NodeLike,
    attribute: str,
    row_index: int,
    column_index: int,
    warnings: list[TableWarning],
) -> int:
    """Read a rowspan/colspan attribute, clamping bad values to something safe.

    Follows the HTML parsing rules for span attributes: the leading integer of
    the value is used (so ``"2.5"`` behaves like ``2``), a value without a
    leading integer is a parse error repaired to 1, zero or a negative number
    is repaired to 1, and anything above the browser-style cap is clamped.
    Every repair records a locatable warning.
    """
    raw = cell.attributes.get(attribute)
    if raw is None:
        return 1

    match = _LEADING_INT.match(raw)
    if match is None:
        warnings.append(
            TableWarning(
                code="invalid-span",
                message=(
                    f"Invalid {attribute} value {raw!r} on <{cell.tag}> at "
                    f"row {row_index}, column {column_index}; treated as 1"
                ),
                row=row_index,
                column=column_index,
                tag=cell.tag,
                attribute=attribute,
            )
        )
        return 1

    value = int(match.group(1))

    # A value with trailing junk (e.g. "2x" or "2.5") still yields its leading
    # integer per the spec, but is non-conformant markup worth flagging.
    if match.group(0).strip() != raw.strip():
        warnings.append(
            TableWarning(
                code="invalid-span",
                message=(
                    f"Non-integer {attribute} value {raw!r} on <{cell.tag}> "
                    f"at row {row_index}, column {column_index}; used the "
                    f"leading integer {value}"
                ),
                row=row_index,
                column=column_index,
                tag=cell.tag,
                attribute=attribute,
            )
        )

    if value <= 0:
        warnings.append(
            TableWarning(
                code="invalid-span",
                message=(
                    f"Non-positive {attribute} value {raw!r} on <{cell.tag}> "
                    f"at row {row_index}, column {column_index}; treated as 1"
                ),
                row=row_index,
                column=column_index,
                tag=cell.tag,
                attribute=attribute,
            )
        )
        return 1

    if value > _MAX_SPAN:
        warnings.append(
            TableWarning(
                code="span-overflow",
                message=(
                    f"{attribute} value {value} on <{cell.tag}> at row "
                    f"{row_index}, column {column_index} exceeds the limit of "
                    f"{_MAX_SPAN}; clamped to {_MAX_SPAN}"
                ),
                row=row_index,
                column=column_index,
                tag=cell.tag,
                attribute=attribute,
            )
        )
        return _MAX_SPAN

    return value


def _direct_children(node: _NodeLike) -> list[_NodeLike]:
    """Return element-only direct children, independent of node.iter flags."""
    return [child for child in node.iter() if child.tag is not None]


def _build_grid(
    ordered_rows: list[tuple],
    warnings: list[TableWarning],
) -> list[TableRow]:
    """Lay ``(tr_node, section)`` pairs out on a fixed-width rectangular grid.

    The input order is preserved while the grid is built so that
    ``rowspan``/``colspan`` claims resolve exactly the way the markup spells
    them, even when the sections are written out of order. The grid is stored
    sparsely: ``occupied`` marks positions already claimed by an earlier row's
    spans, and each row maps a column index to its originating cell's text.
    Materializing the rectangle afterwards turns every unrecorded position
    into the fixed empty string placeholder.
    """
    occupied = set()
    sparse_rows: list[dict] = []
    row_sections: list[str] = []
    width = 0

    for row_index, (tr_node, section) in enumerate(ordered_rows):
        sparse: dict = {}
        column_index = 0
        row_width = 0

        for cell in _direct_children(tr_node):
            if cell.tag not in _CELL_TAGS:
                # Non-cell content inside a row is not part of the grid.
                continue

            # Skip positions a previous rowspan/colspan already claimed.
            while (row_index, column_index) in occupied:
                column_index += 1

            rowspan = _parse_span(cell, "rowspan", row_index, column_index, warnings)
            colspan = _parse_span(cell, "colspan", row_index, column_index, warnings)

            text = _cell_text(cell)
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    target = (row_index + row_offset, column_index + column_offset)
                    if row_offset == 0 and column_offset == 0:
                        sparse[column_index] = text
                    else:
                        occupied.add(target)

            row_width = max(row_width, column_index + colspan)
            column_index += colspan

        # A row can be "extended" solely by a rowspan arriving from above.
        while (row_index, row_width) in occupied:
            row_width += 1

        width = max(width, row_width)
        sparse_rows.append(sparse)
        row_sections.append(section)

    return [
        TableRow(
            section=row_sections[row_index],
            cells=[sparse.get(column_index, "") for column_index in range(width)],
        )
        for row_index, sparse in enumerate(sparse_rows)
    ]


def _extract_table(table_node: _NodeLike, index: int) -> ExtractedTable:
    """Build the rectangular representation of a single table node."""
    warnings: list[TableWarning] = []
    children = _direct_children(table_node)

    captions = [child for child in children if child.tag == "caption"]
    caption = _cell_text(captions[0]) if captions else ""
    if len(captions) > 1:
        warnings.append(
            TableWarning(
                code="multiple-captions",
                message=(
                    f"Table {index} has {len(captions)} <caption> elements; "
                    "using the first one and ignoring the rest"
                ),
                tag="caption",
            )
        )

    # (tr node, section) in the order the rows appear in the markup.
    ordered_rows: list[tuple] = []
    for child in children:
        if child.tag in _SECTION_TAGS:
            for row in _direct_children(child):
                if row.tag == "tr":
                    ordered_rows.append((row, child.tag))
        elif child.tag == "tr":
            # HTML parsers normally wrap loose rows in an implicit tbody;
            # report the section the same way.
            ordered_rows.append((child, "tbody"))
        elif child.tag in _CELL_TAGS:
            # A cell directly under <table> has no row to land in. The HTML
            # parser usually fosters it out, but keep the safety net.
            warnings.append(
                TableWarning(
                    code="cell-outside-row",
                    message=(
                        f"<{child.tag}> is a direct child of <table> but is "
                        "not inside any <tr>; skipped"
                    ),
                    tag=child.tag,
                )
            )

    rows = _build_grid(ordered_rows, warnings)

    # The grid is laid out in markup order so spans resolve against the
    # written structure, but the public record order is the canonical
    # thead -> tbody -> tfoot. Rows keep their materialized cell list, so
    # placeholders computed from a rowspan still line up.
    section_order = {"thead": 0, "tbody": 1, "tfoot": 2}
    rows.sort(key=lambda row: section_order[row.section])
    header_rows = [row for row in rows if row.section == "thead"]

    return ExtractedTable(
        index=index,
        caption=caption,
        rows=rows,
        header_rows=header_rows,
        warnings=warnings,
    )


def extract_tables(root: _NodeLike) -> list[ExtractedTable]:
    """Extract every table in the subtree of ``root`` as rectangular records.

    Tables are returned in document order, so a nested table follows the table
    that contains it. Nested tables are extracted independently: a nested
    table's rows never enter the outer table's grid because grid rows only
    read the direct ``tr``/section children of their own table.

    Parameters
    ----------
    root:
        Node to search beneath, typically ``LexborHTMLParser(...).root``. The
        node itself is only read; the DOM is never modified.

    Returns
    -------
    list of ExtractedTable
        One entry per ``<table>`` found, or an empty list when the subtree
        contains no tables. An unexpected failure on one table is recorded as
        an ``"extraction-error"`` warning on an otherwise empty result instead
        of interrupting the remaining tables.
    """
    table_nodes = [node for node in root.traverse() if node.tag == "table"]
    extracted: list[ExtractedTable] = []

    for index, table_node in enumerate(table_nodes):
        try:
            extracted.append(_extract_table(table_node, index))
        except Exception as exc:  # noqa: BLE001 - one bad table must not abort the rest
            extracted.append(
                ExtractedTable(
                    index=index,
                    warnings=[
                        TableWarning(
                            code="extraction-error",
                            message=(
                                f"Failed to extract table {index}: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            tag="table",
                        )
                    ],
                )
            )

    return extracted
