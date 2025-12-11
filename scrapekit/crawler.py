"""Resumable, concurrent, same-domain crawler."""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
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


def content_hash(items: list[dict], text: str) -> tuple[str, str]:
    """Hash what we care about: recipe items if any, else the page's main text."""
    content = json.dumps(items, sort_keys=True, ensure_ascii=False, indent=1) if items else text
    return hashlib.sha256(content.encode()).hexdigest(), content


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
        items = []
        next_urls = links(doc, final)
        if self.recipe:
            if self.recipe.applies(final):
                items = self.recipe.extract(doc, final)
            follow = self.recipe.follow_links(doc, final)
            if follow is not None:
                next_urls = follow
        h, content = content_hash(items, main_text(doc))
        return {"state": "done", "status": resp.status, "final": final, "title": title(doc),
                "hash": h, "content": content, "items": items, "links": next_urls}

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
            if depth < self.cfg.max_depth:
                seed = st.db.execute("SELECT seed FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                for link in r["links"]:
                    if not self.cfg.same_domain or same_domain(link, seed):
                        st.enqueue(run_id, link, depth + 1)
            log.info("%s %s items=%d", r["status"], url, len(r["items"]))
        else:
            log.info("%s %s (%s)", r["state"], url, r.get("note"))
        st.commit()  # per page, so a killed crawl can resume
