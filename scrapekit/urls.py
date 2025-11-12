"""URL normalization and scoping."""
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize(url: str, base: str | None = None) -> str | None:
    """Return a canonical absolute URL, or None if it isn't http(s).

    Lowercases scheme/host, drops default ports and fragments, resolves
    dot-segments, sorts query params, and normalizes percent-encoding.
    """
    if base:
        url = urljoin(base, url.strip())
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    if parts.port and parts.port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{parts.port}"
    path = _resolve_dots(quote(unquote(parts.path), safe="/%:@!$&'()*+,;=-._~")) or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def _resolve_dots(path: str) -> str:
    out: list[str] = []
    for seg in path.split("/"):
        if seg == "..":
            if len(out) > 1:
                out.pop()
        elif seg != ".":
            out.append(seg)
    if path.endswith(("/.", "/..")):
        out.append("")
    return "/".join(out)


def domain(url: str) -> str:
    return urlsplit(url).netloc.lower()


def same_domain(url: str, root: str) -> bool:
    return domain(url) == domain(root)
