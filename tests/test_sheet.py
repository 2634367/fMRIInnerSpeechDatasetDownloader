import io
import zipfile

from dsdl.sheet import parse_table_html, parse_zip

# Shape of Google's HTML export: column-letter thead, row-number <th> per row, rich links, <br>.
HTML = """
<table class="waffle"><thead><tr><th class="row-header freezebar-origin-ltr"></th><th>A</th><th>B</th><th>C</th></tr></thead>
<tbody>
<tr><th class="row-headers-background"><div class="row-header-wrapper">1</div></th>
  <td class="s0">Title</td><td class="s0">ID</td><td class="s0">Code</td></tr>
<tr><th class="row-headers-background"><div class="row-header-wrapper">2</div></th>
  <td>Bimodal &amp; more</td>
  <td><span><a href="https://openneuro.org/datasets/ds004196/versions/2.0.2">ds004196</a></span> <br>
      <span><a href="https://openneuro.org/datasets/ds006033/versions/1.0.0">ds006033</a></span></td>
  <td><a href="https://www.google.com/url?q=https://github.com/a/b&amp;sa=D">GitHub</a></td></tr>
<tr><th class="row-headers-background"><div class="row-header-wrapper">3</div></th>
  <td class="freezebar-cell"></td><td></td><td></td><td></td></tr>
<tr><th class="row-headers-background"><div class="row-header-wrapper">4</div></th>
  <td colspan="2">Merged title</td><td>x</td></tr>
</tbody></table>
"""


def test_parse_table_html():
    rows = parse_table_html(HTML, "Datasets")
    assert len(rows) == 2
    r = rows[0]
    assert r.number == 2
    assert r.text("title") == "Bimodal & more"
    assert r.text("id") == "ds004196\nds006033"
    assert r.links("id") == [
        "https://openneuro.org/datasets/ds004196/versions/2.0.2",
        "https://openneuro.org/datasets/ds006033/versions/1.0.0",
    ]
    assert r.links("code") == ["https://github.com/a/b"]
    merged = rows[1]
    assert merged.number == 4 and merged.text("title") == "Merged title" and merged.text("code") == "x"


def test_parse_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Datasets.html", HTML)
        z.writestr("Code Examples.html", HTML)
        z.writestr("resources/sheet.css", "")
    tabs = parse_zip(buf.getvalue())
    assert set(tabs) == {"Datasets", "Code Examples"}
