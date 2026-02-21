"""Tiny DOM built on html.parser, plus a CSS selector subset.

Supported selectors:
  tag, *, #id, .class, [attr], [attr=v], [attr^=v], [attr$=v], [attr*=v],
  [attr~=v], :first-child, :last-child, :nth-child(n), descendant (space),
  child (>), and groups (a, b).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# Tags an opening tag of the same kind implicitly closes (<p>a<p>b, <li>..<li>).
AUTOCLOSE = {"p", "li", "option", "tr", "td", "th", "dt", "dd"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None, parent: Node | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent

    @property
    def elements(self) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node)]

    def iter(self):
        """Depth-first over descendant elements (not self)."""
        for c in self.elements:
            yield c
            yield from c.iter()

    def text(self, skip=("script", "style", "noscript", "template")) -> str:
        parts: list[str] = []

        def walk(n: Node):
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in skip:
                    walk(c)
        walk(self)
        return " ".join(" ".join(parts).split())

    def get(self, attr: str, default=None):
        return self.attrs.get(attr, default)

    def select(self, selector: str) -> list[Node]:
        return select(self, selector)

    def select_one(self, selector: str) -> Node | None:
        r = select(self, selector)
        return r[0] if r else None

    def __repr__(self):
        return f"<{self.tag} {self.attrs}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        if tag in AUTOCLOSE and self.cur.tag == tag:
            self.cur = self.cur.parent
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:  # ignore stray end tags
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


# ---------------------------------------------------------------- selectors

_TOKEN = re.compile(r"""
    (?P<ws>\s*>\s*|\s+)                                  # combinator
  | (?P<tag>\*|[a-zA-Z][\w-]*)
  | \#(?P<id>[\w-]+)
  | \.(?P<cls>[\w-]+)
  | \[\s*(?P<attr>[\w:-]+)\s*(?:(?P<op>[~^$*]?=)\s*(?P<val>"[^"]*"|'[^']*'|[^\]\s]+)\s*)?\]
  | :(?P<pseudo>first-child|last-child|nth-child)(?:\(\s*(?P<arg>\d+)\s*\))?
""", re.X)


def _compile(sel: str):
    """Compile one selector (no commas) to a list of (combinator, [predicates])."""
    steps: list[tuple[str, list]] = []
    preds: list = []
    comb = " "
    pos = 0
    sel = sel.strip()
    while pos < len(sel):
        m = _TOKEN.match(sel, pos)
        if not m:
            raise ValueError(f"unsupported selector syntax at {sel[pos:]!r}")
        pos = m.end()
        g = m.groupdict()
        if g["ws"] is not None:
            if preds:
                steps.append((comb, preds))
                preds = []
            comb = ">" if ">" in g["ws"] else " "
        elif g["tag"]:
            t = g["tag"].lower()
            if t != "*":
                preds.append(lambda n, t=t: n.tag == t)
        elif g["id"]:
            preds.append(lambda n, v=g["id"]: n.attrs.get("id") == v)
        elif g["cls"]:
            preds.append(lambda n, v=g["cls"]: v in n.attrs.get("class", "").split())
        elif g["attr"]:
            preds.append(_attr_pred(g["attr"].lower(), g["op"], g["val"]))
        elif g["pseudo"]:
            preds.append(_pseudo_pred(g["pseudo"], g["arg"]))
    if not preds:
        preds.append(lambda n: True)
    steps.append((comb, preds))
    return steps


def _attr_pred(name, op, val):
    if val and val[0] in "\"'":
        val = val[1:-1]
    tests = {
        None: lambda a: True,
        "=": lambda a: a == val,
        "^=": lambda a: a.startswith(val),
        "$=": lambda a: a.endswith(val),
        "*=": lambda a: val in a,
        "~=": lambda a: val in a.split(),
    }
    test = tests[op]
    return lambda n: name in n.attrs and test(n.attrs[name])


def _pseudo_pred(name, arg):
    def index(n):
        sibs = n.parent.elements if n.parent else [n]
        return sibs.index(n), len(sibs)
    if name == "first-child":
        return lambda n: index(n)[0] == 0
    if name == "last-child":
        return lambda n: index(n)[0] == index(n)[1] - 1
    k = int(arg or 1)
    return lambda n: index(n)[0] == k - 1


def _matches(node: Node, steps, i: int) -> bool:
    comb, preds = steps[i]
    if not all(p(node) for p in preds):
        return False
    if i == 0:
        return True
    prev_comb = comb
    anc = node.parent
    if prev_comb == ">":
        return anc is not None and anc.tag != "#document" and _matches(anc, steps, i - 1)
    while anc is not None and anc.tag != "#document":
        if _matches(anc, steps, i - 1):
            return True
        anc = anc.parent
    return False


def select(root: Node, selector: str) -> list[Node]:
    """All descendants of root matching selector, in document order."""
    # ponytail: naive comma split; breaks on commas inside [attr="a,b"]. Tokenize groups if needed.
    groups = [_compile(s) for s in selector.split(",") if s.strip()]
    return [n for n in root.iter()
            if any(_matches(n, steps, len(steps) - 1) for steps in groups)]
