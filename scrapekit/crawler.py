"""Resumable, concurrent, same-domain crawler."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass

from .dom import parse
from .extract import Recipe, links, main_text, title
from .fetch import Fetcher, RobotsDisallowed
from .store import Store
from .urls import normalize, same_domain

log = logging.getLogger("scrapekit")


@dataclass
class CrawlConfig:
    max_depth: int = 2
    max_pages: int = 50
    workers: int = 4
    same_domain: bool = True
    user_agent: str | None = None
    delay: float = 1.0
    retries: int = 3
    respect_robots: bool = True
    recipe_path: str | None = None
    sitemap: bool = False


def content_hash(items: list[dict], text: str) -> tuple[str, str]:
    """Hash what we care about: recipe items if any, else the page's main text."""
    content = json.dumps(items, sort_keys=True, ensure_ascii=False, indent=1) if items else text
    return hashlib.sha256(content.encode()).hexdigest(), content


def discover_sitemap(fetcher: Fetcher, seed: str, limit: int, max_sitemaps: int = 20) -> list[str]:
    """Up to `limit` page URLs from the seed origin's sitemaps.

    Follows sitemap indexes (at most `max_sitemaps` files), accepts gzipped
    sitemaps, and skips any sitemap that is missing, disallowed or malformed.
    """
    todo, seen, urls = fetcher.sitemap_locations(seed), set(), {}
    while todo and len(seen) < max_sitemaps and len(urls) < limit:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            resp = fetcher.fetch(sm)
            body = resp.body
            if body[:2] == b"\x1f\x8b":
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(50_000_000)
            root = ET.fromstring(body)
        except (RobotsDisallowed, urllib.error.URLError, OSError, EOFError, ET.ParseError):
            continue
        if resp.status != 200:
            continue
        locs = [normalize(e.text.strip(), sm) for e in root.iter()
                if e.tag.rsplit("}", 1)[-1] == "loc" and e.text]
        if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
            todo += [u for u in locs if u]
        else:
            urls.update(dict.fromkeys(u for u in locs if u))
        log.info("sitemap %s: %d locs", sm, len(locs))
    return list(urls)[:limit]


class Crawler:
    def __init__(self, store: Store, cfg: CrawlConfig, recipe: Recipe | None = None,
                 fetcher: Fetcher | None = None):
        self.store = store
        self.cfg = cfg
        self.recipe = recipe
        kw = {"delay": cfg.delay, "retries": cfg.retries, "respect_robots": cfg.respect_robots}
        if cfg.user_agent:
            kw["user_agent"] = cfg.user_agent
        self.fetcher = fetcher or Fetcher(**kw)
        # ponytail: pagination hop counts live in memory, so --resume restarts them at 0.
        self._hops: dict[str, int] = {}

    # ------------------------------------------------------------ worker
    def _process(self, url: str) -> dict:
        """Runs in a worker thread. No DB access here."""
        try:
            resp = self.fetcher.fetch(url)
        except RobotsDisallowed:
            return {"state": "skipped", "note": "robots.txt"}
        except (urllib.error.URLError, OSError) as e:
            return {"state": "failed", "note": str(e)}
        if resp.status >= 400:
            return {"state": "failed", "note": f"HTTP {resp.status}", "status": resp.status}
        if not resp.is_html:
            return {"state": "skipped", "note": resp.content_type, "status": resp.status}
        final = normalize(resp.final_url) or url
        doc = parse(resp.text)
        items, pages = [], []
        next_urls = links(doc, final)
        if self.recipe:
            pages = self.recipe.next_pages(doc, final)
            if self.recipe.applies(final):
                items = self.recipe.extract(doc, final)
            follow = self.recipe.follow_links(doc, final)
            if follow is not None:
                next_urls = follow
        h, content = content_hash(items, main_text(doc))
        return {"state": "done", "status": resp.status, "final": final, "title": title(doc),
                "hash": h, "content": content, "items": items, "links": next_urls,
                "pages": pages}

    # ------------------------------------------------------------ driver
    def run(self, seed: str | None = None, resume: bool = False) -> int:
        """Crawl from seed (new run) or resume the latest unfinished run. Returns run id."""
        st = self.store
        if resume:
            row = st.unfinished_run()
            if row is None:
                raise SystemExit("nothing to resume")
            run_id, seed = row["id"], row["seed"]
            st.reset_inflight(run_id)
            log.info("resuming run %s (%s queued)", run_id, st.count(run_id, "queued"))
        else:
            seed = normalize(seed)
            if not seed:
                raise SystemExit("seed must be an http(s) URL")
            run_id = st.new_run(seed, asdict(self.cfg))
            st.enqueue(run_id, seed, 0)
            if self.cfg.sitemap:
                # ponytail: capped at max_pages URLs; sitemap lastmod/priority are ignored.
                for u in discover_sitemap(self.fetcher, seed, self.cfg.max_pages):
                    if not self.cfg.same_domain or same_domain(u, seed):
                        st.enqueue(run_id, u, 0)
            st.commit()

        cfg = self.cfg
        inflight: dict = {}
        with ThreadPoolExecutor(cfg.workers) as pool:
            while True:
                fetched = st.count(run_id, "done", "failed", "inflight")
                room = min(cfg.workers - len(inflight), cfg.max_pages - fetched)
                if room > 0:
                    for url, depth in st.claim(run_id, room):
                        inflight[pool.submit(self._process, url)] = (url, depth)
                if not inflight:
                    break
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    url, depth = inflight.pop(fut)
                    self._record(run_id, url, depth, fut.result())
        st.finish_run(run_id)
        return run_id

    def _record(self, run_id: int, url: str, depth: int, r: dict):
        st = self.store
        st.mark(run_id, url, r["state"], r.get("note"))
        if r["state"] == "done":
            st.save_page(run_id, url, r["status"], r["title"], r["hash"], r["content"],
                         r["items"], self.recipe.name if self.recipe else None)
            seed = st.db.execute("SELECT seed FROM runs WHERE id=?", (run_id,)).fetchone()[0]

            def in_scope(u):
                return not self.cfg.same_domain or same_domain(u, seed)
            limit = self.recipe.max_pagination if self.recipe else None
            hops = self._hops.get(url, 0) + 1
            for page in r["pages"]:  # same depth: paging through a listing isn't going deeper
                if in_scope(page) and (limit is None or hops <= limit) and st.enqueue(run_id, page, depth):
                    self._hops[page] = hops
            if depth < self.cfg.max_depth:
                for link in r["links"]:
                    if in_scope(link):
                        st.enqueue(run_id, link, depth + 1)
            log.info("%s %s items=%d", r["status"], url, len(r["items"]))
        else:
            log.info("%s %s (%s)", r["state"], url, r.get("note"))
        st.commit()  # per page, so a killed crawl can resume
