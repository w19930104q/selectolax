![selectolax logo](docs/logo.png)

---

A fast HTML5 parser with CSS selectors, written in Cython, using the [Lexbor](https://github.com/lexbor/lexbor) engine.

---

[![PyPI - Version](https://img.shields.io/pypi/v/selectolax?logo=pypi&label=Pypi&logoColor=fff)](https://pypi.org/project/selectolax)
[![PyPI Total Downloads](https://static.pepy.tech/badge/selectolax)](https://pepy.tech/projects/selectolax)
[![CI](https://img.shields.io/github/actions/workflow/status/rushter/selectolax/pythonpackage.yml?branch=master&logo=githubactions&label=CI)](https://github.com/rushter/selectolax/actions/workflows/pythonpackage.yml?query=branch%3Amaster+event%3Apush)
[![GitHub License](https://img.shields.io/github/license/rushter/selectolax?logo=github&label=License)](https://github.com/rushter/selectolax/blob/master/LICENSE)

---

## Installation

From PyPI using pip:

```bash
pip install selectolax
```

If installation fails due to compilation errors, you may need to install [Cython](https://github.com/cython/cython):

```bash
pip install selectolax[cython]
```

This usually happens when you try to install an outdated version of selectolax on a newer version of Python.

Development version from GitHub:

```bash
git clone --recursive  https://github.com/rushter/selectolax
cd selectolax
pip install -r requirements_dev.txt
python setup.py install
```

How to compile selectolax while developing:

```bash
make clean
make dev
```

## Basic examples

Here are some basic examples to get you started with selectolax:

Parsing HTML and extracting text:

```python
from selectolax.lexbor import LexborHTMLParser

html = """
<h1 id="title" data-updated="20201101">Hi there</h1>
<div class="post">Lorem Ipsum is simply dummy text of the printing and typesetting industry. </div>
<div class="post">Lorem ipsum dolor sit amet, consectetur adipiscing elit.</div>
"""
tree = LexborHTMLParser(html)

print(tree.css_first('h1#title').text())
# 'Hi there'

print(tree.css_first('h1#title').attributes)
# {'id': 'title', 'data-updated': '20201101'}

print([node.text() for node in tree.css('.post')])
# ['Lorem Ipsum is simply dummy text of the printing and typesetting industry. ',
#  'Lorem ipsum dolor sit amet, consectetur adipiscing elit.']
```

### Using advanced CSS selectors

```python
from selectolax.lexbor import LexborHTMLParser

html = "<div><p id=p1><p id=p2><p id=p3><a>link</a><p id=p4><p id=p5>text<p id=p6></div>"
selector = "div > :nth-child(2n+1):not(:has(a))"

for node in LexborHTMLParser(html).css(selector):
    print(node.attributes, node.text(), node.tag)
    print(node.parent.tag)
    print(node.html)
# {'id': 'p1'}  p
# div
# <p id="p1"></p>
# {'id': 'p5'} text p
# div
# <p id="p5">text</p>
```

#### Using `lexbor-contains` CSS pseudo-class to match text

```python
from selectolax.lexbor import LexborHTMLParser
html = "<div><p>hello </p><p id='main'>lexbor is AwesOme</p></div>"
parser = LexborHTMLParser(html)
# Case-insensitive search
results = parser.css('p:lexbor-contains("awesome" i)')
# Case-sensitive search
results = parser.css('p:lexbor-contains("AwesOme")')
assert len(results) == 1
assert results[0].text() == "lexbor is AwesOme"
```

### Extracting tables as CSV-ready records

`extract_tables()` turns every `<table>` into rectangular, CSV-compatible
records without hand-rolling CSS walks. Rows come out in
`thead` -> `tbody` -> `tfoot` order, `rowspan`/`colspan` cells land in the
grid positions they actually cover (empty strings fill the placeholders),
nested tables are extracted independently, and the parsed DOM is never
modified. Malformed tables produce locatable `warnings` instead of aborting
the other tables.

```python
import csv
import io

from selectolax.lexbor import LexborHTMLParser

html = """
<table>
  <caption>季度销售</caption>
  <thead>
    <tr><th rowspan="2">商品</th><th colspan="2">季度</th></tr>
    <tr><th>Q1</th><th>Q2</th></tr>
  </thead>
  <tbody>
    <tr><td>苹果</td><td>10</td><td>20</td></tr>
  </tbody>
</table>
"""

table = LexborHTMLParser(html).extract_tables()[0]
print(table.caption)
# 季度销售
for row in table.rows:
    print(row.section, row.cells)
# thead ['商品', '季度', '']
# thead ['', 'Q1', 'Q2']
# tbody ['苹果', '10', '20']

# Every row has the same width, so it feeds straight into the csv module.
buffer = io.StringIO()
csv.writer(buffer).writerows(row.cells for row in table.rows)
```

* [More examples](https://selectolax.readthedocs.io/en/latest/examples.html)

## Simple Benchmark

* Extract title, links, scripts and a meta tag from main pages of top 754 domains. See `examples/benchmark.py` for more information.

| Package                       | Time      |
|-------------------------------|-----------|
| Beautiful Soup (html.parser)  | 61.02 sec.|
| lxml / Beautiful Soup (lxml)  | 9.09 sec. |
| html5_parser                  | 16.10 sec.|
| selectolax (Lexbor)           | 2.39 sec. |

## Links

* [Changelog](CHANGES.md)
* [selectolax API reference and examples](https://selectolax.readthedocs.io/en/latest/index.html)
* [Video introduction to web scraping using selectolax](https://youtu.be/HpRsfpPuUzE)
* [How to Scrape 7k Products with Python using selectolax and httpx](https://www.youtube.com/watch?v=XpGvq755J2U)
* [Lexbor benchmark](https://lexborisov.github.io/benchmark-html-parsers/)
* [Python benchmark](https://rushter.com/blog/python-fast-html-parser/)
* [Another Python benchmark](https://www.peterbe.com/plog/selectolax-or-pyquery)
* [Universal interface to lxml and selectolax](https://github.com/lorien/domselect)

## License

* lexbor engine — [Apache-2.0 license](https://github.com/lexbor/lexbor?tab=Apache-2.0-1-ov-file#readme)
* selectolax - [MIT](https://github.com/rushter/selectolax/blob/master/LICENSE)


## Contributors

Thanks to all the contributors of selectolax!

<a href="https://github.com/rushter/selectolax/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=rushter/selectolax" />
</a>
