"""Tiny DOM built on html.parser, plus a CSS selector subset.

Supported selectors:
  tag, *, #id, .class, [attr], [attr=v], [attr^=v], [attr$=v], [attr*=v],
  [attr~=v], :first-child, :last-child, :nth-child(an+b|odd|even),
  :not(selector list), descendant (space), child (>), adjacent sibling (+),
  general sibling (~), and groups (a, b).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# Tags an opening tag of the same kind implicitly closes (<p>a<p>b, <li>..<li>).
AUTOCLOSE = {"p", "li", "option", "tr", "td", "th", "dt", "dd"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "_els", "_pos")

    def __init__(self, tag: str, attrs: dict | None = None, parent: Node | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent
        self._els = self._pos = None  # caches; the tree isn't modified after parse()

    @property
    def elements(self) -> list[Node]:
        if self._els is None:
            self._els = [c for c in self.children if isinstance(c, Node)]
        return self._els

    def index(self) -> tuple[int, int]:
        """(position among the parent's element children, number of them)."""
        if self.parent is None:
            return 0, 1
        par = self.parent
        if par._pos is None:
            par._pos = {id(c): i for i, c in enumerate(par.elements)}
        return par._pos[id(self)], len(par.elements)

    def iter(self):
        """Depth-first over descendant elements (not self)."""
        stack = self.elements[::-1]
        while stack:
            n = stack.pop()
            yield n
            stack += n.elements[::-1]

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
    (?P<ws>\s*[>+~,]\s*|\s+)                             # combinator or group comma
  | (?P<tag>\*|[a-zA-Z][\w-]*)
  | \#(?P<id>[\w-]+)
  | \.(?P<cls>[\w-]+)
  | \[\s*(?P<attr>[\w:-]+)\s*(?:(?P<op>[~^$*]?=)\s*(?P<val>"[^"]*"|'[^']*'|[^\]\s]+)\s*)?\]
  | :(?P<pseudo>[a-z-]+)(?P<paren>\()?
""", re.X)
_NTH = re.compile(r"^(?:(?P<a>[+-]?\d*)n\s*(?:(?P<sign>[+-])\s*(?P<b>\d+))?|(?P<k>[+-]?\d+))$")


def _close_paren(sel: str, pos: int) -> int:
    """Index of the ')' closing the '(' just before pos, skipping quoted strings."""
    depth, quote = 1, None
    for i in range(pos, len(sel)):
        c = sel[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError(f"unbalanced parenthesis in {sel!r}")


def _compile(sel: str):
    """Compile a selector list to groups, each a list of (combinator, [predicates])."""
    groups: list[list[tuple[str, list]]] = []
    steps: list[tuple[str, list]] = []
    preds: list = []
    comb = " "
    pos = 0
    sel = sel.strip()

    # Every compound selector adds at least one predicate (`*` adds match-all),
    # so empty `preds` at a combinator or group end means nothing was there.
    def end_group():
        if not preds:
            raise ValueError(f"empty selector or dangling combinator in {sel!r}")
        steps.append((comb, preds))
        groups.append(list(steps))
        steps.clear()

    while pos < len(sel):
        m = _TOKEN.match(sel, pos)
        if not m:
            raise ValueError(f"unsupported selector syntax at {sel[pos:]!r}")
        pos = m.end()
        g = m.groupdict()
        if g["ws"] is not None:
            c = g["ws"].strip() or " "
            if c == ",":
                end_group()
                preds, comb = [], " "
                continue
            if not preds:
                raise ValueError(f"dangling combinator in {sel!r}")
            steps.append((comb, preds))
            preds, comb = [], c
        elif g["tag"]:
            t = g["tag"].lower()
            preds.append((lambda n: True) if t == "*" else (lambda n, t=t: n.tag == t))
        elif g["id"]:
            preds.append(lambda n, v=g["id"]: n.attrs.get("id") == v)
        elif g["cls"]:
            preds.append(lambda n, v=g["cls"]: v in n.attrs.get("class", "").split())
        elif g["attr"]:
            preds.append(_attr_pred(g["attr"].lower(), g["op"], g["val"]))
        elif g["pseudo"]:
            arg = None
            if g["paren"]:
                close = _close_paren(sel, pos)
                arg, pos = sel[pos:close].strip(), close + 1
            preds.append(_pseudo_pred(g["pseudo"], arg))
    end_group()
    return groups


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


def _nth(arg: str):
    """Parse an :nth-child() argument (an+b, odd, even, k) into a 1-based index test."""
    arg = {"odd": "2n+1", "even": "2n"}.get(arg.lower(), arg.lower())
    m = _NTH.match(arg.replace(" ", "")) if arg else None
    if not m:
        raise ValueError(f"bad :nth-child() argument {arg!r}")
    if m["k"] is not None:
        a, b = 0, int(m["k"])
    else:
        a = int(m["a"] + "1" if m["a"] in ("", "+", "-") else m["a"])
        b = int(m["sign"] + m["b"]) if m["b"] else 0
    if a == 0:
        return lambda i: i == b
    return lambda i: (i - b) % a == 0 and (i - b) // a >= 0


def _pseudo_pred(name, arg):
    if name == "first-child" and arg is None:
        return lambda n: n.index()[0] == 0
    if name == "last-child" and arg is None:
        return lambda n: n.index()[0] == n.index()[1] - 1
    if name == "not" and arg:
        groups = _compile(arg)
        return lambda n: not any(_matches(n, g, len(g) - 1) for g in groups)
    if name == "nth-child" and arg is not None:
        test = _nth(arg)
        return lambda n: test(n.index()[0] + 1)
    raise ValueError(f"unsupported pseudo-class :{name}" + (f"({arg})" if arg is not None else ""))


def _prev_siblings(node: Node) -> list[Node]:
    """Element siblings before node, nearest first."""
    if node.parent is None:
        return []
    return node.parent.elements[: node.index()[0]][::-1]


def _matches(node: Node, steps, i: int) -> bool:
    comb, preds = steps[i]
    if not all(p(node) for p in preds):
        return False
    if i == 0:
        return True
    if comb == "+":
        prev = _prev_siblings(node)[:1]
        return bool(prev) and _matches(prev[0], steps, i - 1)
    if comb == "~":
        return any(_matches(s, steps, i - 1) for s in _prev_siblings(node))
    anc = node.parent
    if comb == ">":
        return anc is not None and anc.tag != "#document" and _matches(anc, steps, i - 1)
    while anc is not None and anc.tag != "#document":
        if _matches(anc, steps, i - 1):
            return True
        anc = anc.parent
    return False


def select(root: Node, selector: str) -> list[Node]:
    """All descendants of root matching selector, in document order."""
    groups = _compile(selector)
    return [n for n in root.iter()
            if any(_matches(n, steps, len(steps) - 1) for steps in groups)]
