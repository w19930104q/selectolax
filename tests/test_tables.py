"""Regression tests for ``LexborHTMLParser.extract_tables()``.

Two layers are exercised:

* end-to-end behavior through the parsed Lexbor DOM, including the HTML
  parser's own fostering/reparenting of malformed table markup;
* the pure-Python grid engine in ``selectolax.table_extraction`` through
  lightweight in-memory fakes, which can represent structures the HTML parser
  itself would repair (e.g. a cell directly under ``<table>``).
"""

import csv
import io

import pytest

from selectolax.lexbor import LexborHTMLParser
from selectolax.table_extraction import (
    ExtractedTable,
    TableRow,
    TableWarning,
    extract_tables,
)

# ---------------------------------------------------------------------------
# Minimal in-memory DOM used to drive the engine without a parser
# ---------------------------------------------------------------------------


class FakeNode:
    """A read-only stand-in exposing the slice of ``LexborNode`` used here.

    Mirrors the real DOM shape: an element never holds text directly, its
    text lives in text-node children (``tag is None``).
    """

    def __init__(self, tag=None, text="", children=None, attributes=None):
        self.tag = tag
        self._text = text
        self.children = list(children or [])
        self.attributes = dict(attributes or {})
        if tag is not None and text:
            self.children.insert(0, FakeNode(tag=None, text=text))
            self._text = ""

    def iter(self, include_text=False, skip_empty=False):
        return iter(list(self.children))

    def traverse(self, include_text=False, skip_empty=False):
        yield self
        for child in self.children:
            yield from child.traverse()

    @property
    def is_text_node(self):
        return self.tag is None

    @property
    def text_content(self):
        # Matches LexborNode.text_content: text only on text nodes.
        return self._text if self.tag is None else None

    def text(self, deep=True, separator="", strip=False, skip_empty=False):
        if self.tag is None:
            return self._text
        parts = []
        if self._text:
            parts.append(self._text)
        for child in self.children:
            if not deep and child.tag is None:
                parts.append(child._text)
            elif deep:
                parts.append(child.text(deep=True, separator=separator))
        return separator.join(parts)


def cell(tag, text, **attrs):
    # DOM attributes are always strings, mirror that for the fakes.
    return FakeNode(
        tag=tag, text=text, attributes={k: str(v) for k, v in attrs.items()}
    )


def row(*cells, section=False):
    return FakeNode(tag="tr", children=list(cells))


def section(tag, *rows):
    return FakeNode(tag=tag, children=list(rows))


def table(*children, caption=None):
    nodes = []
    if caption is not None:
        if isinstance(caption, str):
            nodes.append(FakeNode(tag="caption", text=caption))
        else:
            nodes.extend(caption)
    nodes.extend(children)
    return FakeNode(tag="table", children=nodes)


def grid_of(extracted):
    return [row.cells for row in extracted.rows]


# ---------------------------------------------------------------------------
# Parser-level behavior
# ---------------------------------------------------------------------------


def test_no_tables_returns_empty_list():
    assert LexborHTMLParser("<div><p>no tables here</p></div>").extract_tables() == []
    assert LexborHTMLParser("").extract_tables() == []
    assert LexborHTMLParser("plain text").extract_tables() == []


def test_empty_table_returns_empty_rows_without_warnings():
    extracted = LexborHTMLParser("<table></table>").extract_tables()
    assert len(extracted) == 1
    result = extracted[0]
    assert isinstance(result, ExtractedTable)
    assert result.index == 0
    assert result.caption == ""
    assert result.rows == []
    assert result.header_rows == []
    assert result.warnings == []


def test_simple_table_rows_are_rectangular_strings():
    parser = LexborHTMLParser(
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td></td></tr></table>"
    )
    result = parser.extract_tables()[0]
    assert grid_of(result) == [["a", "b"], ["c", ""]]
    assert all(row.section == "tbody" for row in result.rows)
    # Empty placeholders are fixed to empty strings, never None.
    assert all(isinstance(value, str) for r in result.rows for value in r.cells)


def test_multi_level_header_with_rowspan_and_colspan():
    html = """
    <table>
      <caption>商品清单</caption>
      <thead>
        <tr>
          <th rowspan="2">商品</th>
          <th colspan="2">规格</th>
          <th rowspan="2">价格</th>
        </tr>
        <tr><th>颜色</th><th>尺寸</th></tr>
      </thead>
      <tbody>
        <tr><td>衬衫</td><td>白</td><td>L</td><td>¥99</td></tr>
        <tr><td>裤子</td><td colspan="2">均码</td><td>¥199</td></tr>
      </tbody>
      <tfoot>
        <tr><td colspan="3">合计</td><td>¥298</td></tr>
      </tfoot>
    </table>
    """
    result = LexborHTMLParser(html).extract_tables()[0]

    assert result.caption == "商品清单"
    assert grid_of(result) == [
        ["商品", "规格", "", "价格"],
        ["", "颜色", "尺寸", ""],
        ["衬衫", "白", "L", "¥99"],
        ["裤子", "均码", "", "¥199"],
        ["合计", "", "", "¥298"],
    ]
    assert [r.section for r in result.rows] == [
        "thead",
        "thead",
        "tbody",
        "tbody",
        "tfoot",
    ]
    assert [r.cells for r in result.header_rows] == grid_of(result)[:2]
    assert all(len(r.cells) == 4 for r in result.rows)
    assert result.warnings == []


def test_rowspan_fills_actual_grid_positions():
    html = """
    <table>
      <tr><td rowspan="2">r</td><td>1</td></tr>
      <tr><td>2</td></tr>
      <tr><td>a</td><td>b</td></tr>
    </table>
    """
    result = LexborHTMLParser(html).extract_tables()[0]
    assert grid_of(result) == [["r", "1"], ["", "2"], ["a", "b"]]


def test_irregular_rows_are_padded_to_a_rectangle():
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
    result = LexborHTMLParser(html).extract_tables()[0]
    assert grid_of(result) == [["a", "b", "c"], ["d", "", ""]]


def test_sections_are_emitted_in_canonical_order():
    # tfoot appears before tbody in the markup; output must still be
    # thead -> tbody -> tfoot while rows keep their own cell lists.
    html = (
        "<table>"
        "<thead><tr><th>h</th></tr></thead>"
        "<tfoot><tr><td>f</td></tr></tfoot>"
        "<tbody><tr><td>b</td></tr></tbody>"
        "</table>"
    )
    result = LexborHTMLParser(html).extract_tables()[0]
    assert [r.section for r in result.rows] == ["thead", "tbody", "tfoot"]
    assert grid_of(result) == [["h"], ["b"], ["f"]]
    assert [r.cells for r in result.header_rows] == [["h"]]


def test_nested_tables_are_independent_and_in_document_order():
    html = """
    <table id="outer">
      <tr>
        <td>outer-a</td>
        <td>
          <table id="inner">
            <tr><td>in-1</td><td>in-2</td></tr>
          </table>
        </td>
      </tr>
      <tr><td>outer-b</td><td>outer-c</td></tr>
    </table>
    """
    tables = LexborHTMLParser(html).extract_tables()
    assert len(tables) == 2
    outer, inner = tables
    assert outer.index == 0
    assert inner.index == 1
    # The inner table's cells must not enter the outer grid.
    assert grid_of(outer) == [["outer-a", ""], ["outer-b", "outer-c"]]
    assert grid_of(inner) == [["in-1", "in-2"]]


def test_deeply_nested_tables_keep_document_order():
    html = (
        "<table><tr><td>L0"
        "<table><tr><td>L1"
        "<table><tr><td>L2</td></tr></table>"
        "</td></tr></table>"
        "</td></tr></table>"
    )
    tables = LexborHTMLParser(html).extract_tables()
    assert [t.index for t in tables] == [0, 1, 2]
    assert grid_of(tables[2]) == [["L2"]]


def test_chinese_cells_and_caption_round_trip_through_csv():
    html = """
    <table>
      <thead><tr><th>名称</th><th>备注</th></tr></thead>
      <tbody>
        <tr><td>苹果</td><td>红,甜</td></tr>
        <tr><td>牛奶</td><td></td></tr>
      </tbody>
    </table>
    """
    result = LexborHTMLParser(html).extract_tables()[0]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row_ in result.header_rows:
        writer.writerow(row_.cells)
    for row_ in result.rows:
        if row_.section != "thead":
            writer.writerow(row_.cells)

    decoded = list(csv.reader(io.StringIO(buffer.getvalue())))
    assert decoded == [["名称", "备注"], ["苹果", "红,甜"], ["牛奶", ""]]


def test_whitespace_is_collapsed_like_rendered_text():
    parser = LexborHTMLParser(
        "<table><tr>"
        "<td>  hello\n\tworld  </td>"
        "<td><span>a</span> <span>b</span></td>"
        "<td></td>"
        "</tr></table>"
    )
    result = parser.extract_tables()[0]
    assert result.rows[0].cells == ["hello world", "a b", ""]


def test_caption_text_is_normalized():
    parser = LexborHTMLParser(
        "<table><caption>  标题\n第二行  </caption><tr><td>x</td></tr></table>"
    )
    assert parser.extract_tables()[0].caption == "标题 第二行"


def test_cell_text_keeps_document_order_across_nested_elements():
    html = "<table><tr><td>before <b>bold <i>italic</i></b> after</td></tr></table>"
    result = LexborHTMLParser(html).extract_tables()[0]
    assert result.rows[0].cells == ["before bold italic after"]


def test_cell_text_skips_nested_table_at_any_depth():
    html = (
        "<table><tr><td>"
        "outer-before "
        "<div><table><tr><td>INNER</td></tr></table></div>"
        "outer-after"
        "</td></tr></table>"
    )
    tables = LexborHTMLParser(html).extract_tables()
    assert "INNER" not in tables[0].rows[0].cells[0]
    assert tables[0].rows[0].cells[0].split() == ["outer-before", "outer-after"]
    assert grid_of(tables[1]) == [["INNER"]]


def test_deeply_nested_cell_text_does_not_recurse():
    # The text walk is iterative; this depth would overflow the C stack with
    # a recursive implementation.
    depth = 3000
    inner = "x"
    for _ in range(depth):
        inner = f"<div>{inner}</div>"
    parser = LexborHTMLParser(f"<table><tr><td>{inner}</td></tr></table>")
    assert parser.extract_tables()[0].rows[0].cells == ["x"]


def test_invalid_span_values_produce_locatable_warnings():
    html = """
    <table>
      <tr>
        <td colspan="abc" rowspan="-2">x</td>
        <td>y</td>
      </tr>
      <tr><td colspan="0">z</td></tr>
    </table>
    """
    result = LexborHTMLParser(html).extract_tables()[0]
    codes = [warning.code for warning in result.warnings]
    assert codes.count("invalid-span") == 3
    for warning in result.warnings:
        assert isinstance(warning, TableWarning)
        assert warning.attribute in {"rowspan", "colspan"}
        assert warning.tag == "td"
        assert warning.row is not None and warning.column is not None
    # Bad spans are repaired to 1 and the grid stays rectangular.
    assert all(len(r.cells) == len(result.rows[0].cells) for r in result.rows)


def test_oversized_span_is_clamped_with_warning():
    parser = LexborHTMLParser('<table><tr><td colspan="5000">x</td></tr></table>')
    result = parser.extract_tables()[0]
    warning = result.warnings[0]
    assert warning.code == "span-overflow"
    assert "5000" in warning.message and "1000" in warning.message
    assert len(result.rows[0].cells) == 1000


def test_span_attributes_follow_the_html_leading_integer_rules():
    # A second normal cell makes the grid width span + 1, so each case can be
    # checked by how wide the first cell expands.
    def span_width(attribute_html):
        html = f"<table><tr><td {attribute_html}>x</td><td>y</td></tr></table>"
        result = LexborHTMLParser(html).extract_tables()[0]
        return len(result.rows[0].cells), [w.code for w in result.warnings]

    # Leading integer is honoured even with trailing junk, with a warning.
    assert span_width('colspan="2.5"') == (3, ["invalid-span"])
    assert span_width('colspan="2x"') == (3, ["invalid-span"])
    # Leading zero-padded integer is valid without a warning.
    assert span_width('colspan="05"') == (6, [])
    assert span_width('colspan="3"') == (4, [])
    # No leading integer, or an explicit zero, is repaired to 1.
    assert span_width('colspan="abc"') == (2, ["invalid-span"])
    assert span_width('colspan="0"') == (2, ["invalid-span"])
    assert span_width('rowspan="-3"') == (2, ["invalid-span"])
    # Values past the cap are clamped.
    width, warnings = span_width('colspan="1001"')
    assert width == 1001 and warnings == ["span-overflow"]


def test_one_bad_table_does_not_interrupt_others():
    html = (
        "<table><tr><td>good-1</td></tr></table>"
        "<table><tr><td colspan='nope'>bad</td></tr></table>"
        "<table><tr><td>good-2</td></tr></table>"
    )
    tables = LexborHTMLParser(html).extract_tables()
    assert [t.index for t in tables] == [0, 1, 2]
    assert grid_of(tables[0]) == [["good-1"]]
    assert grid_of(tables[2]) == [["good-2"]]
    assert [w.code for w in tables[1].warnings] == ["invalid-span"]


def test_multiple_captions_warn_and_use_first():
    html = (
        "<table>"
        "<caption>first</caption><caption>second</caption>"
        "<tr><td>x</td></tr>"
        "</table>"
    )
    result = LexborHTMLParser(html).extract_tables()[0]
    assert result.caption == "first"
    assert result.warnings[0].code == "multiple-captions"
    assert result.warnings[0].tag == "caption"


def test_extraction_does_not_modify_the_dom():
    html = (
        "<table><thead><tr><th>h</th></tr></thead>"
        "<tbody><tr><td rowspan='2'>a</td><td>b</td></tr>"
        "<tr><td>c</td></tr></tbody></table>"
    )
    parser = LexborHTMLParser(html)
    before = parser.html
    parser.extract_tables()
    parser.extract_tables()
    assert parser.html == before


def test_works_on_fragment_parsers():
    parser = LexborHTMLParser(
        "<table><tr><td>片段</td><td>x</td></tr></table>", is_fragment=True
    )
    result = parser.extract_tables()[0]
    assert grid_of(result) == [["片段", "x"]]


def test_multiple_tables_have_sequential_indexes():
    parser = LexborHTMLParser(
        "<table><tr><td>1</td></tr></table><p>gap</p><table><tr><td>2</td></tr></table>"
    )
    assert [t.index for t in parser.extract_tables()] == [0, 1]


def test_th_in_tbody_is_a_data_row_not_a_header_row():
    parser = LexborHTMLParser("<table><tr><th>label</th><td>value</td></tr></table>")
    result = parser.extract_tables()[0]
    assert result.header_rows == []
    assert all(r.section == "tbody" for r in result.rows)
    assert grid_of(result) == [["label", "value"]]


def test_result_dataclass_shapes():
    result = LexborHTMLParser(
        "<table><thead><tr><th>h</th></tr></thead>"
        "<tbody><tr><td>d</td></tr></tbody></table>"
    ).extract_tables()[0]
    assert isinstance(result.rows[0], TableRow)
    assert result.header_rows[0] is result.rows[0]
    assert isinstance(result.warnings, list)


# ---------------------------------------------------------------------------
# Engine-level tests through the in-memory fakes
# ---------------------------------------------------------------------------


def test_engine_cell_outside_row_warning():
    # The HTML parser would foster this into a row; drive the engine directly
    # to exercise the defensive branch.
    stray = cell("td", "stray")
    tree = table(stray, row(cell("td", "a")))
    result = extract_tables(tree)[0]
    codes = {warning.code for warning in result.warnings}
    assert "cell-outside-row" in codes
    assert grid_of(result) == [["a"]]


def test_engine_extraction_error_is_contained(monkeypatch):
    import selectolax.table_extraction as engine

    tree = table(row(cell("td", "x")))

    def broken_build(rows, warnings):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_build_grid", broken_build)
    result = engine.extract_tables(tree)
    assert len(result) == 1
    assert result[0].rows == []
    assert result[0].warnings[0].code == "extraction-error"
    assert "boom" in result[0].warnings[0].message


def test_engine_records_section_even_without_elements():
    tree = table(row(cell("td", "loose")))
    result = extract_tables(tree)[0]
    assert result.rows[0].section == "tbody"


def test_engine_nested_table_cell_text_does_not_leak():
    inner = table(section("tbody", row(cell("td", "inner-text"))))
    outer = table(
        section(
            "tbody",
            row(cell("td", "outer-text"), FakeNode(tag="td", children=[inner])),
        )
    )
    result = extract_tables(outer)
    assert grid_of(result[0]) == [["outer-text", ""]]
    assert grid_of(result[1]) == [["inner-text"]]


def test_engine_rowspan_then_following_cells_skip_occupied_positions():
    tree = table(
        section(
            "tbody",
            row(cell("td", "r", rowspan=2), cell("td", "a")),
            row(cell("td", "b"), cell("td", "c")),
        )
    )
    result = extract_tables(tree)[0]
    # Two source rows; the second row's first column is the rowspan
    # placeholder, so its cells shift right into columns 1 and 2.
    assert len(result.rows) == 2
    assert grid_of(result) == [["r", "a", ""], ["", "b", "c"]]
    assert all(len(r.cells) == 3 for r in result.rows)


def test_engine_plain_text_children_are_ignored_in_rows():
    text_node = FakeNode(tag=None, text="noise")
    tree = table(
        section("tbody", FakeNode(tag="tr", children=[text_node, cell("td", "x")]))
    )
    result = extract_tables(tree)[0]
    assert grid_of(result) == [["x"]]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
