import unittest

from scrapekit.dom import parse

HTML = """<html><head><title>T &amp; U</title></head><body>
<div id="main" class="wrap big">
  <ul><li class="a">one<li class="b">two<li>three</ul>
  <p>para <b>bold</b><br>after<p>second
  <a href="/x" data-k="v1 v2">X</a>
  <section><a href="https://ext/y">Y</a></section>
</div>
<script>var s = "<a href='no'>";</script>
</body></html>"""


class TestDom(unittest.TestCase):
    def setUp(self):
        self.doc = parse(HTML)

    def sel(self, s):
        return [n.text() for n in self.doc.select(s)]

    def test_basic(self):
        self.assertEqual(self.sel("title"), ["T & U"])
        self.assertEqual(self.sel("li"), ["one", "two", "three"])
        self.assertEqual(self.sel("#main > p")[0], "para bold after")

    def test_autoclose_and_void(self):
        self.assertEqual(len(self.doc.select("li")), 3)
        self.assertEqual(len(self.doc.select("p")), 2)
        self.assertEqual(self.doc.select("p")[0].text(), "para bold after")

    def test_classes_ids_attrs(self):
        self.assertEqual(self.sel("div.wrap.big > ul > li.b"), ["two"])
        self.assertEqual(self.sel("a[href^=https]"), ["Y"])
        self.assertEqual(self.sel("a[data-k~=v2]"), ["X"])
        self.assertEqual(self.sel("a[href$='/x']"), ["X"])
        self.assertEqual(self.sel("a[href*=ext]"), ["Y"])

    def test_combinators_and_groups(self):
        self.assertEqual(self.sel("section a, li.a"), ["one", "Y"])  # document order
        self.assertEqual(self.sel("#main > a"), [])  # a is inside p (auto-closed later)
        self.assertEqual(self.sel("div a"), ["X", "Y"])

    def test_pseudos(self):
        self.assertEqual(self.sel("li:first-child"), ["one"])
        self.assertEqual(self.sel("li:last-child"), ["three"])
        self.assertEqual(self.sel("li:nth-child(2)"), ["two"])

    def test_script_not_parsed_or_texted(self):
        self.assertEqual(len(self.doc.select("a")), 2)
        self.assertNotIn("var s", self.doc.text())

    def test_bad_selector(self):
        with self.assertRaises(ValueError):
            self.doc.select("a::before")


if __name__ == "__main__":
    unittest.main()
