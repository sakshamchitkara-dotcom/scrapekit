"""Change detection between two crawl runs, with stdout/webhook alerts."""
from __future__ import annotations

import difflib
import json
import urllib.request

from .store import Store


def pick_runs(store: Store, old: int | None = None, new: int | None = None) -> tuple[int, int]:
    """Default: the latest finished run and the previous finished run of the same seed."""
    for given in (old, new):
        if given is not None:
            store.run(given)  # exits if unknown
    runs = store.runs()
    if new is None:
        if not runs:
            raise SystemExit("no finished runs")
        new = runs[-1]["id"]
    if old is None:
        seed = next((r["seed"] for r in runs if r["id"] == new), None)
        prev = [r["id"] for r in runs if r["seed"] == seed and r["id"] < new]
        if not prev:
            raise SystemExit(f"no earlier run of {seed} to diff run {new} against")
        old = prev[-1]
    return old, new


def diff_runs(store: Store, old: int, new: int, context: int = 1) -> dict:
    a, b = store.pages(old), store.pages(new)
    changed = []
    for url in sorted(a.keys() & b.keys()):
        if a[url]["hash"] != b[url]["hash"]:
            lines = difflib.unified_diff(
                (a[url]["content"] or "").splitlines(), (b[url]["content"] or "").splitlines(),
                f"run{old}", f"run{new}", n=context, lineterm="")
            changed.append({"url": url, "diff": "\n".join(lines)})
    return {
        "old_run": old, "new_run": new,
        "added": sorted(b.keys() - a.keys()),
        "removed": sorted(a.keys() - b.keys()),
        "changed": changed,
    }


def has_changes(d: dict) -> bool:
    return bool(d["added"] or d["removed"] or d["changed"])


def format_report(d: dict) -> str:
    out = [f"diff run {d['old_run']} -> run {d['new_run']}: "
           f"{len(d['added'])} added, {len(d['removed'])} removed, {len(d['changed'])} changed"]
    out += [f"+ {u}" for u in d["added"]]
    out += [f"- {u}" for u in d["removed"]]
    for c in d["changed"]:
        out.append(f"~ {c['url']}")
        out += ["    " + line for line in c["diff"].splitlines()]
    return "\n".join(out)


def post_webhook(url: str, d: dict, timeout: float = 10) -> int:
    """POST the diff as JSON (Slack-compatible: includes a 'text' field)."""
    body = json.dumps({"text": format_report(d), **d}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                              "User-Agent": "scrapekit-alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status
