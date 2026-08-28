"""The crawl orchestrator: frontier, worker pool, manifest, finalise pass."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib import robotparser
from urllib.parse import urlsplit

import requests
from selenium.common.exceptions import WebDriverException

from . import report
from .assets import AssetManager
from .browser import RenderedPage, apply_cookies, build_driver, render
from .config import Config
from .rewriter import (collect_assets, extract_links, make_soup, rewrite_page)
from .urls import (PathAllocator, canonicalize, in_scope, looks_like_page,
                   page_relpath)

log = logging.getLogger("crawler")

ROBOTS_SKIP = "blocked by robots.txt"
HTML_TYPES = ("text/html", "application/xhtml", "application/xml", "text/xml", "")


@dataclass
class PageRecord:
    key: str
    url: str
    final_url: str = ""
    path: str = ""
    raw: str = ""
    title: str = ""
    status: Optional[int] = None
    depth: int = 0
    parent: Optional[str] = None
    links: List[str] = field(default_factory=list)
    bytes: int = 0
    fetched_at: str = ""
    error: Optional[str] = None


# --------------------------------------------------------------------------
class Frontier:
    """Depth-ordered work queue that knows when the crawl is really finished."""

    def __init__(self, max_pages: int):
        self._queue: Deque[Tuple[int, str, str, Optional[str]]] = deque()
        self._seen: Set[str] = set()
        self._condition = threading.Condition()
        self._active = 0
        self._closed = False
        self.max_pages = max_pages
        self.accepted = 0
        self.deferred: Dict[str, int] = {}   # key -> depth, discovered but not queued

    def add(self, key: str, url: str, depth: int, parent: Optional[str]) -> bool:
        with self._condition:
            if key in self._seen or self._closed:
                return False
            if self.max_pages and self.accepted >= self.max_pages:
                self.deferred.setdefault(key, depth)
                return False
            self._seen.add(key)
            self.accepted += 1
            self._queue.append((depth, key, url, parent))
            self._condition.notify()
            return True

    def mark_seen(self, key: str) -> None:
        with self._condition:
            self._seen.add(key)

    def refund(self) -> None:
        """Give a page-budget slot back (the URL turned out to be uncrawlable)."""
        with self._condition:
            self.accepted = max(0, self.accepted - 1)

    def defer(self, key: str, depth: int) -> None:
        with self._condition:
            if key not in self._seen:
                self.deferred.setdefault(key, depth)

    def next(self) -> Optional[Tuple[int, str, str, Optional[str]]]:
        with self._condition:
            while True:
                if self._closed:
                    return None
                if self._queue:
                    self._active += 1
                    return self._queue.popleft()
                if self._active == 0:
                    self._condition.notify_all()
                    return None
                self._condition.wait(timeout=0.5)

    def task_done(self) -> None:
        with self._condition:
            self._active -= 1
            if self._active == 0 and not self._queue:
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def size(self) -> int:
        with self._condition:
            return len(self._queue)


# --------------------------------------------------------------------------
class RobotsCache:
    def __init__(self, user_agent: str, enabled: bool):
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            parser = self._parsers.get(origin, "missing")
        if parser == "missing":
            parser = self._load(origin)
            with self._lock:
                self._parsers[origin] = parser
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def _load(self, origin: str) -> Optional[robotparser.RobotFileParser]:
        try:
            response = requests.get(f"{origin}/robots.txt", timeout=10,
                                    headers={"User-Agent": self.user_agent})
            if response.status_code >= 400:
                return None
            parser = robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
            log.info("robots.txt loaded for %s", origin)
            return parser
        except Exception as exc:
            log.debug("robots.txt unavailable for %s (%s)", origin, exc)
            return None


# --------------------------------------------------------------------------
class Crawler:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.pages: Dict[str, PageRecord] = {}
        self.paths = PathAllocator()
        self.assets = AssetManager(cfg)
        self.frontier = Frontier(cfg.max_pages)
        self.robots = RobotsCache(cfg.user_agent, cfg.respect_robots)
        self.lock = threading.Lock()
        self.start_key = ""      # this run's entry point
        self.origin_key = ""     # the very first crawl's entry point
        self.started_at = datetime.now(timezone.utc)
        self._cookies: List[dict] = []
        self._stop = threading.Event()
        self._counter = 0

    # -- persistence -------------------------------------------------------
    def load_manifest(self) -> bool:
        path = self.cfg.manifest_path
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read manifest (%s) -- starting fresh", exc)
            return False

        for key, raw in data.get("pages", {}).items():
            raw.pop("key", None)
            self.pages[key] = PageRecord(key=key, **raw)
        self.assets.load(data.get("assets", {}))
        self.paths.load({k: p.path for k, p in self.pages.items() if p.path})
        self.origin_key = data.get("start_key", "") or self.origin_key
        for key, depth in (data.get("not_crawled") or {}).items():
            self.frontier.deferred.setdefault(key, depth)
        log.info("resuming: %d pages and %d assets already on disk",
                 len(self.pages), self.assets.count)
        return True

    def save_manifest(self) -> None:
        with self.lock:
            pages = {k: {kk: vv for kk, vv in asdict(v).items() if kk != "key"}
                     for k, v in self.pages.items()}
        payload = {
            "version": 1,
            "start_url": self.cfg.start_url,
            "start_key": self.origin_key or self.start_key,
            "created": self.started_at.isoformat(),
            "finished": datetime.now(timezone.utc).isoformat(),
            "settings": {
                "max_depth": self.cfg.max_depth,
                "max_pages": self.cfg.max_pages,
                "include_subdomains": self.cfg.include_subdomains,
                "path_prefix": self.cfg.path_prefix,
                "download_assets": self.cfg.download_assets,
                "respect_robots": self.cfg.respect_robots,
                "workers": self.cfg.workers,
            },
            "pages": pages,
            "assets": self.assets.snapshot(),
            "not_crawled": dict(self.frontier.deferred),
        }
        tmp = self.cfg.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.cfg.manifest_path)

    # -- crawling ----------------------------------------------------------
    def run(self) -> None:
        canon = canonicalize(self.cfg.start_url, keep_query=self.cfg.keep_query)
        if not canon:
            raise SystemExit(f"Not a crawlable URL: {self.cfg.start_url}")
        start_url, self.start_key = canon

        if self.cfg.resume:
            self.load_manifest()
            # Re-queue anything that failed or was never reached last time.
            for key, record in list(self.pages.items()):
                if record.error:
                    self.pages.pop(key)
            for key, record in self.pages.items():
                self.frontier.mark_seen(key)
                self.frontier.accepted += 1
            for key, rec in list(self.pages.items()):
                for link in rec.links:
                    if link not in self.pages:
                        target = self._url_for_key(link)
                        if target:
                            self.frontier.add(link, target, rec.depth + 1, key)

        self.origin_key = self.origin_key or self.start_key
        self.frontier.add(self.start_key, start_url, 0, None)

        workers = max(1, self.cfg.workers)
        log.info("crawling %s (depth<=%d, max %d pages, %d worker%s)",
                 start_url, self.cfg.max_depth, self.cfg.max_pages,
                 workers, "" if workers == 1 else "s")

        threads = [threading.Thread(target=self._worker, args=(i,), daemon=True,
                                    name=f"worker-{i}") for i in range(workers)]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                while thread.is_alive():
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            log.warning("interrupted -- finishing up with what we have")
            self._stop.set()
            self.frontier.close()
            for thread in threads:
                thread.join(timeout=10)

    def _url_for_key(self, key: str) -> Optional[str]:
        canon = canonicalize(key, keep_query=self.cfg.keep_query)
        return canon[0] if canon else None

    def _worker(self, index: int) -> None:
        driver = None
        try:
            driver = build_driver(self.cfg)
            if self.cfg.cookies_file and self.cfg.cookies_file.exists():
                cookies = json.loads(self.cfg.cookies_file.read_text("utf-8"))
                origin = f"{urlsplit(self.cfg.start_url).scheme}://{urlsplit(self.cfg.start_url).netloc}/"
                apply_cookies(driver, cookies, origin)
            if self.cfg.manual_login and index == 0:
                self._manual_login(driver)
            with self.lock:
                shared = list(self._cookies)
            if shared:
                origin = f"{urlsplit(self.cfg.start_url).scheme}://{urlsplit(self.cfg.start_url).netloc}/"
                apply_cookies(driver, shared, origin)

            while not self._stop.is_set():
                item = self.frontier.next()
                if item is None:
                    break
                depth, key, url, parent = item
                try:
                    self._process(driver, key, url, depth, parent)
                except Exception as exc:
                    log.error("unexpected failure on %s: %s", url, exc, exc_info=self.cfg.verbose)
                    self._record_failure(key, url, depth, parent, str(exc))
                finally:
                    self.frontier.task_done()
                if self.cfg.delay:
                    time.sleep(self.cfg.delay)
        except WebDriverException as exc:
            log.error("worker %d could not start a browser: %s", index, str(exc)[:300])
            self._stop.set()
            self.frontier.close()
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _manual_login(self, driver) -> None:
        driver.get(self.cfg.start_url)
        print("\n" + "=" * 68)
        print("  MANUAL LOGIN")
        print("  Sign in / dismiss dialogs in the browser window, then come back")
        print("  here and press ENTER to start crawling.")
        print("=" * 68)
        input("  Press ENTER when ready... ")
        with self.lock:
            self._cookies = driver.get_cookies()
        log.info("captured %d cookies from the logged-in session", len(self._cookies))

    def _process(self, driver, key: str, url: str, depth: int,
                 parent: Optional[str]) -> None:
        if self.cfg.resume and key in self.pages and not self.pages[key].error:
            return
        if not self.robots.allowed(url):
            log.debug("robots.txt disallows %s -- skipping", url)
            # Not a failure, and it must not count against --max-pages.
            self.frontier.refund()
            self._record_failure(key, url, depth, parent, ROBOTS_SKIP)
            return

        page = render(driver, url, self.cfg)
        if page.error and not page.html:
            log.warning("[d%d] FAILED %s (%s)", depth, url, page.error)
            self._record_failure(key, url, depth, parent, page.error)
            return

        # Chrome happily "renders" PDFs and images; store those as assets instead.
        ctype = (page.content_type or "").split(";")[0].lower()
        if ctype and not any(ctype.startswith(t) for t in HTML_TYPES if t):
            log.info("[d%d] not HTML (%s) -> saving as asset: %s", depth, ctype, url)
            self.assets.fetch(url)
            self.frontier.mark_seen(key)
            return

        self.assets.sync_cookies(page.cookies, urlsplit(url).hostname or "")

        soup = make_soup(page.html)
        relpath = self.paths.allocate(key, page_relpath(url))
        raw_name = f"{hashlib.sha1(key.encode()).hexdigest()}.html"
        raw_rel = f"_crawl/raw/{raw_name}"
        (self.cfg.output_dir / raw_rel).write_text(page.html, "utf-8", errors="replace")

        if self.cfg.download_assets:
            collect_assets(soup, page.final_url or url, self.cfg, self.assets)

        links = extract_links(soup, page.final_url or url, self.cfg)

        record = PageRecord(
            key=key, url=url, final_url=page.final_url or url, path=relpath,
            raw=raw_rel, title=page.title, status=page.status, depth=depth,
            parent=parent, links=links, bytes=len(page.html),
            fetched_at=datetime.now(timezone.utc).isoformat(), error=page.error,
        )
        with self.lock:
            self.pages[key] = record
            self._counter += 1
            index = self._counter

        status = page.status if page.status is not None else "?"
        log.info("[%3d] d%d %s  %s  (%d links, queue %d)", index, depth, status,
                 (page.title or url)[:70], len(links), self.frontier.size)

        if depth < self.cfg.max_depth:
            for link_key in links:
                target = self._url_for_key(link_key)
                if target:
                    self.frontier.add(link_key, target, depth + 1, key)
        else:
            for link_key in links:
                self.frontier.defer(link_key, depth + 1)

        if index % 25 == 0:
            self.save_manifest()

    def _record_failure(self, key: str, url: str, depth: int,
                        parent: Optional[str], error: str) -> None:
        with self.lock:
            self.pages[key] = PageRecord(
                key=key, url=url, depth=depth, parent=parent, error=error,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

    # -- finalise ----------------------------------------------------------
    def finalize(self) -> None:
        """Second pass: rewrite every saved page now that all paths are known."""
        page_map = {k: r.path for k, r in self.pages.items() if r.path and not r.error}
        asset_map = self.assets.path_map()
        written, failed = 0, 0

        log.info("rewriting %d pages for offline browsing...", len(page_map))
        for key, record in self.pages.items():
            if record.error or not record.path or not record.raw:
                continue
            raw_file = self.cfg.output_dir / record.raw
            if not raw_file.exists():
                failed += 1
                continue
            try:
                html = raw_file.read_text("utf-8", errors="replace")
                rewritten = rewrite_page(
                    html, record.final_url or record.url, record.path, self.cfg,
                    page_map, asset_map, sitemap_path="index.html",
                )
                target = self.cfg.output_dir / record.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rewritten, "utf-8", errors="replace")
                written += 1
            except Exception as exc:
                log.error("rewrite failed for %s: %s", record.url, exc,
                          exc_info=self.cfg.verbose)
                failed += 1

        log.info("wrote %d pages%s", written, f" ({failed} failed)" if failed else "")

        report.write_sitemap(self.cfg, self.pages, self.assets, self.frontier.deferred,
                             self.origin_key or self.start_key, self.started_at)
        self.save_manifest()

        if not self.cfg.keep_raw:
            import shutil
            shutil.rmtree(self.cfg.raw_root, ignore_errors=True)

    # -- summary -----------------------------------------------------------
    def summary(self) -> Dict[str, object]:
        ok = [p for p in self.pages.values() if not p.error]
        blocked = [p for p in self.pages.values() if p.error == ROBOTS_SKIP]
        bad = [p for p in self.pages.values() if p.error and p.error != ROBOTS_SKIP]
        return {
            "pages": len(ok),
            "failed": len(bad),
            "blocked": len(blocked),
            "assets": self.assets.count,
            "asset_bytes": self.assets.total_bytes,
            "not_crawled": len(self.frontier.deferred),
            "max_depth_seen": max((p.depth for p in ok), default=0),
            "failures": [(p.url, p.error) for p in bad][:20],
        }
