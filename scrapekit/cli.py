"""scrapekit command-line interface."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import fields

from . import __version__
from . import __doc__ as _pkg_doc
from .crawler import CrawlConfig, Crawler
from .diff import diff_runs, format_report, has_changes, pick_runs, post_webhook
from .export import to_csv, to_jsonl, to_sqlite
from .extract import Recipe, generic, lint
from .fetch import DEFAULT_UA, Fetcher, RobotsDisallowed
from .store import Store


def cmd_crawl(a) -> int:
    store = Store(a.db)
    if a.resume:
        if a.url:
            sys.exit("--resume continues the latest unfinished run; drop the URL")
        row = store.unfinished_run()
        if row is None:
            sys.exit("nothing to resume")
        known = {f.name for f in fields(CrawlConfig)}
        cfg = CrawlConfig(**{k: v for k, v in json.loads(row["config"]).items() if k in known})
    else:
        if not a.url:
            sys.exit("crawl needs a URL (or --resume)")
        cfg = CrawlConfig(max_depth=a.max_depth, max_pages=a.max_pages, workers=a.workers,
                          same_domain=not a.all_domains, user_agent=a.user_agent,
                          delay=a.delay, retries=a.retries, recipe_path=a.recipe,
                          sitemap=a.sitemap, conditional=not a.no_conditional,
                          domain_delays=dict(a.domain_delay or []))
    recipe = Recipe.load(cfg.recipe_path) if cfg.recipe_path else None
    try:
        run = Crawler(store, cfg, recipe, proxy=a.proxy).run(a.url, resume=a.resume)
    except KeyboardInterrupt:  # every finished page is already committed
        store.close()
        print(f"\ninterrupted; continue with: scrapekit crawl --resume --db {a.db}", file=sys.stderr)
        return 130
    st = store.stats(run)
    counts = st["states"]
    print(f"run {run}: done={counts.get('done', 0)} failed={counts.get('failed', 0)} "
          f"skipped={counts.get('skipped', 0)} unvisited={counts.get('queued', 0)} "
          f"items={st['items']} db={a.db}")
    print(stats_line(st))
    store.close()
    return 0


def host_delay(s: str) -> tuple[str, float]:
    host, sep, secs = s.rpartition("=")
    try:
        if sep and host and float(secs) >= 0:
            return host.strip().lower(), float(secs)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"expected HOST=SECONDS, got {s!r}")


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def stats_line(st: dict) -> str:
    codes = " ".join(f"{k}={v}" for k, v in st["status_codes"].items()) or "none"
    t = st["timing_ms"]
    timing = f"p50 {t['p50']} ms, p95 {t['p95']} ms, max {t['max']} ms" if t["count"] else "no timings"
    return f"  http {codes} | {_size(st['bytes'])} | {timing} | {st['duration_s']} s"


def cmd_extract(a) -> int:
    f = Fetcher(user_agent=a.user_agent or DEFAULT_UA, delay=0, proxy=a.proxy)
    try:
        resp = f.fetch(a.url)
    except RobotsDisallowed as e:
        sys.exit(f"disallowed by robots.txt: {a.url}" if e.reason == "robots.txt"
                 else f"not fetched, {e.reason}: {a.url}")
    if resp.status >= 400:
        sys.exit(f"HTTP {resp.status}: {a.url}")
    if a.recipe:
        out = Recipe.load(a.recipe).extract(resp.text, resp.final_url)
    else:
        out = generic(resp.text, resp.final_url)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def cmd_lint(a) -> int:
    """Validate recipes offline; with --url, also report how often each field matched."""
    failed = False
    for path in a.recipes:
        try:
            spec = Recipe.read_spec(path)
        except (OSError, ValueError) as e:  # missing file, bad JSON/YAML
            errors, warnings = [str(e)], []
        else:
            errors, warnings = lint(spec)
        failed |= bool(errors)
        for w in warnings:
            print(f"{path}: warning: {w}")
        for e in errors:
            print(f"{path}: error: {e}")
        if errors:
            continue
        recipe = Recipe(spec)
        print(f"{path}: ok ({len(recipe.fields)} fields)")
        if a.url:
            failed |= not _lint_against(recipe, a.url, a.user_agent)
    return 1 if failed else 0


def _lint_against(recipe: Recipe, url: str, user_agent: str | None) -> bool:
    """Print per-field match counts on one page. False if the page can't be checked."""
    try:
        resp = Fetcher(user_agent=user_agent or DEFAULT_UA, delay=0).fetch(url)
    except RobotsDisallowed as e:
        print(f"  {url}: disallowed by {e.reason}" if e.reason == "robots.txt"
              else f"  {url}: not fetched, {e.reason}")
        return False
    except OSError as e:  # includes URLError
        print(f"  {url}: {e}")
        return False
    if not recipe.applies(resp.final_url):
        print(f"  {url}: HTTP {resp.status}, URL does not match the recipe's `match`")
        return True
    items = recipe.extract(resp.text, resp.final_url)
    print(f"  {url}: HTTP {resp.status}, {len(items)} items")
    for k in recipe.fields:
        n = sum(i.get(k) not in (None, []) for i in items)
        print(f"    {k:<16} {n}/{len(items)}" + ("   <- never matched" if items and not n else ""))
    return True


def cmd_diff(a) -> int:
    store = Store(a.db)
    old, new = pick_runs(store, a.old, a.new)
    d = diff_runs(store, old, new)
    store.close()
    print(json.dumps(d, indent=2) if a.json else format_report(d))
    if a.webhook and has_changes(d):
        try:
            status = post_webhook(a.webhook, d)
        except OSError as e:  # URLError, timeouts, refused connections
            print(f"webhook failed: {e}", file=sys.stderr)
            return 2  # the alert didn't go out: louder than "changes found"
        print(f"webhook {'failed' if status >= 400 else 'sent'}: HTTP {status}", file=sys.stderr)
        if status >= 400:
            return 2
    return 1 if a.exit_code and has_changes(d) else 0


def cmd_export(a) -> int:
    store = Store(a.db)
    runs = store.runs()
    if not a.run and not runs:
        sys.exit("no finished runs to export")
    run = store.run(a.run)["id"] if a.run else runs[-1]["id"]
    if a.pages:
        rows = [dict(r) for r in store.db.execute(
            "SELECT url, status, fetched_at, title, hash FROM pages WHERE run_id=? ORDER BY url", (run,))]
    else:
        rows = store.items(run)
    store.close()
    if a.format == "sqlite":
        if not a.output:
            sys.exit("--format sqlite needs -o PATH")
        to_sqlite(rows, a.output, "pages" if a.pages else "items")
    else:
        fh = open(a.output, "w", newline="", encoding="utf-8") if a.output else sys.stdout
        (to_csv if a.format == "csv" else to_jsonl)(rows, fh)
        if a.output:
            fh.close()
    print(f"exported {len(rows)} rows from run {run}", file=sys.stderr)
    return 0


def cmd_stats(a) -> int:
    store = Store(a.db)
    runs = store.runs(finished_only=False)
    if not runs:
        sys.exit("no runs")
    st = store.stats(a.run or runs[-1]["id"])
    store.close()
    if a.json:
        print(json.dumps(st, indent=2))
        return 0
    s = st["states"]
    print(f"run {st['run']}: {st['seed']}")
    print(f"  pages   done={s.get('done', 0)} failed={s.get('failed', 0)} "
          f"skipped={s.get('skipped', 0)} unvisited={s.get('queued', 0) + s.get('inflight', 0)} "
          f"items={st['items']}")
    print(stats_line(st))
    for r in st["slowest"]:
        print(f"  slow    {r['ms']:>8} ms  {r['status']}  {r['url']}")
    return 0


def cmd_runs(a) -> int:
    store = Store(a.db)
    rows = [dict(r) for r in store.db.execute(
        "SELECT r.id, r.seed, r.started, r.finished,"
        " (SELECT COUNT(*) FROM frontier f WHERE f.run_id=r.id AND f.state='done') AS pages,"
        " (SELECT COUNT(*) FROM items i WHERE i.run_id=r.id) AS items"
        " FROM runs r ORDER BY r.id")]
    store.close()
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started"]))
        state = "unfinished" if r["finished"] is None else f"{r['finished'] - r['started']:.1f} s"
        print(f"{r['id']:>4}  {started}  {state:>10}  pages={r['pages']:<5} items={r['items']:<6} {r['seed']}")
    if not rows:
        print("no runs", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scrapekit", description=_pkg_doc)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="log each fetched URL")
    p.add_argument("--log-json", action="store_true",
                   help="write log lines to stderr as JSON objects (with -v: one per page)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def db(sp):
        sp.add_argument("--db", default="scrapekit.db", help="sqlite database (default: %(default)s)")

    def proxy(sp):
        sp.add_argument("--proxy", metavar="URL",
                        help="HTTP proxy for all requests, e.g. http://user:pass@host:3128 "
                             "(default: HTTP_PROXY/HTTPS_PROXY from the environment; not saved)")

    c = sub.add_parser("crawl", help="crawl a site from a seed URL")
    c.add_argument("url", nargs="?")
    db(c)
    c.add_argument("--recipe", help="JSON/YAML extraction recipe")
    c.add_argument("--max-depth", type=int, default=2)
    c.add_argument("--max-pages", type=int, default=50)
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--delay", type=float, default=1.0, help="min seconds between requests per domain")
    c.add_argument("--domain-delay", type=host_delay, action="append", metavar="HOST=SECONDS",
                   help="delay for one host instead of --delay (repeatable; robots.txt "
                        "Crawl-delay still wins if higher)")
    c.add_argument("--retries", type=int, default=3)
    c.add_argument("--user-agent")
    c.add_argument("--all-domains", action="store_true", help="follow links off the seed's domain")
    c.add_argument("--sitemap", action="store_true",
                   help="also seed from robots.txt Sitemap: entries and /sitemap.xml")
    c.add_argument("--no-conditional", action="store_true",
                   help="always refetch in full instead of revalidating with ETag/Last-Modified")
    c.add_argument("--resume", action="store_true", help="resume the latest unfinished run")
    proxy(c)
    c.set_defaults(fn=cmd_crawl)

    e = sub.add_parser("extract", help="fetch one URL and print extracted JSON")
    e.add_argument("url")
    e.add_argument("--recipe")
    e.add_argument("--user-agent")
    proxy(e)
    e.set_defaults(fn=cmd_extract)

    lt = sub.add_parser("lint", help="validate recipes (keys, selectors, regexes, process steps)")
    lt.add_argument("recipes", nargs="+", metavar="RECIPE")
    lt.add_argument("--url", help="also fetch this page and count how often each field matches")
    lt.add_argument("--user-agent")
    lt.set_defaults(fn=cmd_lint)

    d = sub.add_parser("diff", help="show changes between two runs")
    db(d)
    d.add_argument("--old", type=int)
    d.add_argument("--new", type=int)
    d.add_argument("--json", action="store_true")
    d.add_argument("--webhook", help="POST changes as JSON to this URL (exit 2 if that fails)")
    d.add_argument("--exit-code", action="store_true", help="exit 1 when changes are found")
    d.set_defaults(fn=cmd_diff)

    t = sub.add_parser("stats", help="status codes, bytes and timings of a run")
    db(t)
    t.add_argument("--run", type=int, help="run id (default: latest)")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_stats)

    rn = sub.add_parser("runs", help="list runs (id, start time, duration, pages, items, seed)")
    db(rn)
    rn.add_argument("--json", action="store_true")
    rn.set_defaults(fn=cmd_runs)

    x = sub.add_parser("export", help="export items (or pages) of a run")
    db(x)
    x.add_argument("--run", type=int, help="run id (default: latest finished)")
    x.add_argument("--format", choices=["jsonl", "csv", "sqlite"], default="jsonl")
    x.add_argument("-o", "--output")
    x.add_argument("--pages", action="store_true", help="export page records instead of items")
    x.set_defaults(fn=cmd_export)
    return p


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, msg, plus a page's run/url/state/status/... fields."""
    FIELDS = ("run", "url", "state", "status", "items", "ms", "note")

    def format(self, record):
        out = {"ts": round(record.created, 3), "level": record.levelname.lower(),
               "msg": record.getMessage()}
        out.update({k: getattr(record, k) for k in self.FIELDS if getattr(record, k, None) is not None})
        return json.dumps(out, ensure_ascii=False)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if a.log_json else
                         logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO if a.verbose else logging.WARNING)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
