"""Generic extractors and declarative recipe extraction."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .dom import Node, parse
from .urls import normalize


# ------------------------------------------------------------------ generic

def title(doc: Node) -> str | None:
    n = doc.select_one("title")
    return n.text() if n else None


def meta(doc: Node) -> dict[str, str]:
    out = {}
    for m in doc.select("meta[content]"):
        key = m.get("name") or m.get("property") or m.get("itemprop")
        if key:
            out[key.lower()] = m.get("content")
    return out


def links(doc: Node, base: str) -> list[str]:
    b = doc.select_one("base[href]")
    base = normalize(b.get("href"), base) or base if b else base
    seen, out = set(), []
    for a in doc.select("a[href]"):
        if "nofollow" in a.get("rel", "").split():
            continue
        u = normalize(a.get("href"), base)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def json_ld(doc: Node) -> list:
    out = []
    for s in doc.select('script[type="application/ld+json"]'):
        raw = "".join(c for c in s.children if isinstance(c, str)).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


_BLOCKS = ("article", "main", "section", "div", "td", "body")
_BOILER = re.compile(r"nav|menu|footer|header|sidebar|comment|share|promo|breadcrumb|cookie", re.I)


def main_text(doc: Node) -> str:
    """Readability-style heuristic: pick the block whose own <p> children carry
    the most text, penalized by link density and boilerplate-ish class/id.
    """
    # ponytail: single-block pick; no sibling merging like full Readability.
    best, best_score = None, 0.0
    for n in doc.iter():
        if n.tag not in _BLOCKS:
            continue
        paras = [c.text() for c in n.elements if c.tag in ("p", "pre", "blockquote")]
        text_len = sum(len(p) for p in paras)
        if not text_len:
            continue
        link_len = sum(len(a.text()) for a in n.select("a"))
        all_len = len(n.text()) or 1
        score = text_len * (1 - link_len / all_len) + 25 * len(paras)
        if n.tag in ("article", "main"):
            score *= 1.5
        if _BOILER.search(n.get("class", "") + " " + n.get("id", "")):
            score *= 0.2
        if score > best_score:
            best, best_score = n, score
    if best is None:
        body = doc.select_one("body") or doc
        return body.text()
    return "\n\n".join(c.text() for c in best.elements if c.tag in ("p", "pre", "blockquote", "h1", "h2", "h3"))


def generic(html: str, url: str) -> dict:
    doc = parse(html)
    return {
        "url": url,
        "title": title(doc),
        "meta": meta(doc),
        "links": links(doc, url),
        "json_ld": json_ld(doc),
        "main_text": main_text(doc),
    }


# ------------------------------------------------------------------ recipes

_PSEUDO = re.compile(r"::(text|attr\(([\w:-]+)\))\s*$")
_TYPES = {"str": str, "int": int, "float": float}


class Recipe:
    """A declarative extraction recipe. See README for the format."""

    def __init__(self, spec: dict):
        self.name = spec.get("name", "recipe")
        self.match = re.compile(spec["match"]) if spec.get("match") else None
        self.item = spec.get("item")
        self.fields = {k: self._field(v) for k, v in spec.get("fields", {}).items()}
        self.follow = spec.get("follow")  # list of selectors; None = all links
        if not self.fields:
            raise ValueError("recipe needs at least one field")

    @staticmethod
    def _field(v) -> dict:
        f = {"selector": v} if isinstance(v, str) else dict(v)
        if "selector" not in f:
            raise ValueError(f"field spec missing selector: {v!r}")
        if f.get("type", "str") not in _TYPES:
            raise ValueError(f"unknown type {f['type']!r}")
        m = _PSEUDO.search(f["selector"])
        f["css"] = f["selector"][: m.start()] if m else f["selector"]
        f["attr"] = m.group(2) if m else None
        f["regex"] = re.compile(f["regex"]) if f.get("regex") else None
        return f

    @classmethod
    def load(cls, path: str | Path) -> Recipe:
        text = Path(path).read_text()
        if str(path).endswith((".yml", ".yaml")):
            try:
                import yaml  # optional dependency
            except ImportError as e:
                raise SystemExit("YAML recipes need PyYAML: pip install 'scrapekit[yaml]'") from e
            return cls(yaml.safe_load(text))
        return cls(json.loads(text))

    def applies(self, url: str) -> bool:
        return self.match is None or bool(self.match.search(url))

    def extract(self, html: str | Node, url: str) -> list[dict]:
        doc = parse(html) if isinstance(html, str) else html
        scopes = doc.select(self.item) if self.item else [doc]
        items = []
        for scope in scopes:
            rec = {k: self._value(scope, f, url) for k, f in self.fields.items()}
            if any(v not in (None, []) for v in rec.values()):
                rec["_url"] = url
                items.append(rec)
        return items

    def follow_links(self, doc: Node, url: str) -> list[str] | None:
        if self.follow is None:
            return None
        out = []
        for sel in self.follow:
            for a in doc.select(sel):
                u = normalize(a.get("href", ""), url)
                if u:
                    out.append(u)
        return out

    @staticmethod
    def _value(scope: Node, f: dict, url: str):
        nodes = scope.select(f["css"]) if f["css"].strip() else [scope]
        vals = []
        for n in nodes:
            if f["attr"]:
                v = n.get(f["attr"])
                if v is not None and f["attr"] in ("href", "src"):
                    v = normalize(v, url) or v
            else:
                v = n.text()
            if v is None:
                continue
            if f["regex"]:
                m = f["regex"].search(v)
                if not m:
                    continue
                v = m.group(1) if m.groups() else m.group(0)
            try:
                v = _TYPES[f.get("type", "str")](v)
            except ValueError:
                continue
            vals.append(v)
            if not f.get("many"):
                break
        if f.get("many"):
            return vals
        return vals[0] if vals else f.get("default")
