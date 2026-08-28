"""The crawl orchestrator: frontier, worker pool, manifest, finalise pass."""
from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
import re
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

from . import challenge as challenge_mod
from . import report
from .assets import AssetManager
from .browser import (RenderedPage, apply_cookies, build_driver,
                      build_solver_driver, render)
from .config import Config
from .rewriter import (collect_assets, extract_links, make_soup, rewrite_page)
from .urls import (PathAllocator, canonicalize, in_scope, looks_like_page,
                   page_relpath)

log = logging.getLogger("crawler")

ROBOTS_SKIP = "blocked by robots.txt"
CHALLENGE_PREFIX = "challenge: "
HTML_TYPES = ("text/html", "application/xhtml", "application/xml", "text/xml", "")

# Statuses worth another go: the site is up but is asking us to wait.
RETRY_STATUSES = {429, 500, 502, 503, 504}


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


_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)


def _get_text(url: str, user_agent: str, timeout: int = 15) -> str:
    """GET a URL as text, transparently un-gzipping .xml.gz sitemaps."""
    response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
    if response.status_code >= 400:
        return ""
    body = response.content
    if body[:2] == b"\x1f\x8b":          # gzip magic
        import gzip
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def sitemap_urls(start_url: str, user_agent: str, limit: int = 5000) -> List[str]:
    """Collect the URLs a site publishes in its sitemap(s).

    A sitemap is the site telling you exactly what it wants crawled -- better
    coverage than guessing from links, and far fewer requests to discover it.
    Sitemap indexes are followed one level down, which covers the usual layout.
    """
    parts = urlsplit(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"

    candidates: List[str] = []
    try:
        robots = _get_text(f"{origin}/robots.txt", user_agent)
        candidates.extend(_ROBOTS_SITEMAP_RE.findall(robots))
    except Exception:
        pass
    candidates.append(f"{origin}/sitemap.xml")

    found: List[str] = []
    seen_maps: Set[str] = set()
    queue = list(dict.fromkeys(candidates))
    while queue and len(found) < limit:
        sitemap = queue.pop(0)
        if sitemap in seen_maps:
            continue
        seen_maps.add(sitemap)
        try:
            body = _get_text(sitemap, user_agent)
        except Exception as exc:
            log.debug("sitemap %s unavailable (%s)", sitemap, exc)
            continue
        if not body:
            continue
        locs = [html_module.unescape(loc) for loc in _LOC_RE.findall(body)]
        if not locs:
            continue
        # A <sitemapindex> lists more sitemaps; a <urlset> lists real pages.
        if "<sitemapindex" in body[:2000].lower():
            if len(seen_maps) <= 1:      # only descend one level
                queue.extend(locs[:50])
            continue
        found.extend(locs)
        log.info("sitemap %s -> %d URLs", sitemap, len(locs))

    return found[:limit]


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
class HostThrottle:
    """Per-host pacing that backs off when a site pushes back.

    Getting blocked is usually self-inflicted: four workers with a 0.4s delay
    is ~10 req/s, which plenty of sites treat as an attack.  Slowing down on
    the first 429 keeps the crawl alive where retrying at full speed would get
    the whole IP banned.
    """

    MAX_MULTIPLIER = 32.0

    def __init__(self, base_delay: float):
        self.base = max(0.05, base_delay)
        self._lock = threading.Lock()
        self._multiplier: Dict[str, float] = {}
        self._next_free: Dict[str, float] = {}

    def wait(self, host: str) -> None:
        """Sleep until this host is due for another request."""
        with self._lock:
            gap = self.base * self._multiplier.get(host, 1.0)
            now = time.monotonic()
            due = max(now, self._next_free.get(host, 0.0))
            self._next_free[host] = due + gap
        if due > now:
            time.sleep(due - now)

    def penalise(self, host: str) -> float:
        """The host complained -- double our spacing and report the new gap."""
        with self._lock:
            current = min(self.MAX_MULTIPLIER, self._multiplier.get(host, 1.0) * 2)
            self._multiplier[host] = current
            return self.base * current

    def relax(self, host: str) -> None:
        """A clean response -- drift back towards full speed."""
        with self._lock:
            current = self._multiplier.get(host, 1.0)
            if current > 1.0:
                self._multiplier[host] = max(1.0, current / 1.15)

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return {h: m for h, m in self._multiplier.items() if m > 1.0}


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
        self.throttle = HostThrottle(cfg.delay)
        self.gate = challenge_mod.ChallengeGate(
            driver_factory=lambda: build_solver_driver(cfg),
            start_url=cfg.start_url,
            enabled=cfg.solve_challenges,
            auto_wait=cfg.challenge_wait,
        )
        self.lock = threading.Lock()
        self.start_key = ""      # this run's entry point
        self.origin_key = ""     # the very first crawl's entry point
        self.started_at = datetime.now(timezone.utc)
        self._cookies: List[dict] = []
        self._stop = threading.Event()
        self._counter = 0
        self.walls_hit = 0
        self._session: List[dict] = []   # freshest cookies seen for the start host

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

        if self.cfg.from_sitemap:
            self._seed_from_sitemap(start_url)

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

    def _seed_from_sitemap(self, start_url: str) -> None:
        """Queue whatever the site publishes in its own sitemap."""
        try:
            urls = sitemap_urls(start_url, self.cfg.user_agent)
        except Exception as exc:
            log.debug("sitemap lookup failed (%s)", exc)
            return

        added = 0
        for raw in urls:
            canon = canonicalize(raw, keep_query=self.cfg.keep_query)
            if not canon:
                continue
            target, key = canon
            if not in_scope(target, self.cfg) or not looks_like_page(target):
                continue
            # Depth 1, so --max-depth still means "hops from the start page".
            if self.frontier.add(key, target, 1, self.start_key):
                added += 1
        if added:
            log.info("sitemap seeded %d URLs into the queue", added)

    def _remember_session(self, cookies: List[dict], host: str) -> None:
        """Keep the freshest cookies for the start host.

        Without this, --save-cookies would only ever have something to write
        when a wall was cleared -- but the ordinary session (logins, consent,
        any clearance we were given) is worth keeping too.
        """
        start_host = self.cfg.start_host or urlsplit(self.cfg.start_url).hostname or ""
        if not cookies or host != start_host:
            return
        with self.lock:
            self._session = cookies

    def _save_session(self) -> None:
        """Write the session out so the next run starts already past the wall."""
        target = self.cfg.save_cookies
        if not target:
            return
        # Merge oldest-to-newest by name: whatever a live worker last saw wins,
        # with the cleared-wall and manual-login cookies underneath it.
        merged: Dict[str, dict] = {}
        for source in (self.gate.cookies(), self._cookies, self._session):
            for cookie in source or []:
                name = cookie.get("name")
                if name:
                    merged[name] = cookie
        cookies = list(merged.values())
        if not cookies:
            log.info("no session cookies to save")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cookies, indent=2), "utf-8")
        log.info("saved %d cookies to %s -- reuse with --cookies %s",
                 len(cookies), target, target)

    def _url_for_key(self, key: str) -> Optional[str]:
        canon = canonicalize(key, keep_query=self.cfg.keep_query)
        return canon[0] if canon else None

    @property
    def _origin(self) -> str:
        parts = urlsplit(self.cfg.start_url)
        return f"{parts.scheme}://{parts.netloc}/"

    def _worker(self, index: int) -> None:
        driver = None
        # Cookie generation this driver has loaded.  The gate bumps its own
        # counter whenever a wall is cleared, and we re-sync when they differ.
        epoch = 0
        try:
            driver = build_driver(self.cfg)
            if self.cfg.cookies_file and self.cfg.cookies_file.exists():
                cookies = json.loads(self.cfg.cookies_file.read_text("utf-8"))
                apply_cookies(driver, cookies, self._origin)
                log.info("worker %d loaded %d saved cookies", index, len(cookies))
                if index == 0:
                    self.gate.seed(cookies)
            if self.cfg.manual_login and index == 0:
                self._manual_login(driver)
            with self.lock:
                shared = list(self._cookies)
            if shared:
                apply_cookies(driver, shared, self._origin)
            epoch = self.gate.epoch

            while not self._stop.is_set():
                item = self.frontier.next()
                if item is None:
                    break
                depth, key, url, parent = item
                try:
                    epoch = self._process(driver, key, url, depth, parent, epoch)
                except Exception as exc:
                    log.error("unexpected failure on %s: %s", url, exc, exc_info=self.cfg.verbose)
                    self._record_failure(key, url, depth, parent, str(exc))
                finally:
                    self.frontier.task_done()
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
        self.gate.seed(self._cookies)
        log.info("captured %d cookies from the logged-in session", len(self._cookies))

    def _sync_cookies(self, driver, epoch: int) -> int:
        """Adopt the session from a wall somebody cleared since we last looked."""
        if self.gate.epoch <= epoch:
            return epoch
        cookies = self.gate.cookies()
        if cookies:
            apply_cookies(driver, cookies, self._origin)
            log.debug("worker adopted %d cookies from a cleared session", len(cookies))
        return self.gate.epoch

    def _fetch(self, driver, url: str, depth: int,
               epoch: int) -> Tuple[Optional[RenderedPage], int]:
        """Render one URL, backing off through rate limits and pausing for walls.

        A challenge that could not be cleared is left on the returned page for
        the caller to record -- we never pretend the wall was the content.
        """
        host = urlsplit(url).hostname or ""
        page: Optional[RenderedPage] = None
        slowdowns = 0     # times this host told us to back off
        clears = 0        # times we asked you to clear a wall for this page

        while True:
            # Park while someone has a solver window open, then take whatever
            # session it earned before we spend a request.
            self.gate.wait_until_open()
            epoch = self._sync_cookies(driver, epoch)
            self.throttle.wait(host)

            page = render(driver, url, self.cfg)

            # A wall a person could clear takes priority over the retry check:
            # Cloudflare interstitials are served as 503, so testing the status
            # first would silently retry them instead of ever asking you.
            wall = page.challenge
            clearable = wall is not None and wall.human_clearable
            rate_limited = not clearable and (
                page.status in RETRY_STATUSES
                or (wall is not None and wall.kind == challenge_mod.RATE_LIMIT))

            if rate_limited:
                if slowdowns >= self.cfg.max_retries:
                    page.error = page.error or (
                        page.challenge.as_error if page.challenge else f"http {page.status}")
                    return page, epoch
                slowdowns += 1
                spacing = self.throttle.penalise(host)
                pause = self.cfg.backoff * (2 ** (slowdowns - 1))
                log.warning("%s is rate-limiting us (%s) -- waiting %.0fs, "
                            "spacing now %.1fs/request", host,
                            page.status or "rate limit", pause, spacing)
                time.sleep(pause)
                continue

            if wall is not None:
                with self.lock:
                    self.walls_hit += 1
                if not clearable or not self.gate.enabled or clears >= 2:
                    log.warning("[d%d] %s -- cannot get past this: %s", depth, wall, url)
                    return page, epoch
                log.warning("[d%d] %s -- pausing for you: %s", depth, wall, url)
                if self.gate.clear_wall(url, wall, epoch):
                    clears += 1
                    epoch = self._sync_cookies(driver, epoch)
                    continue
                return page, epoch

            self.throttle.relax(host)

            # Nothing came back at all -- worth one more go before giving up.
            if page.error and not page.html and slowdowns < self.cfg.max_retries:
                slowdowns += 1
                time.sleep(self.cfg.backoff)
                continue
            return page, epoch

    def _process(self, driver, key: str, url: str, depth: int,
                 parent: Optional[str], epoch: int) -> int:
        """Fetch, save and expand one page. Returns the worker's cookie epoch."""
        if self.cfg.resume and key in self.pages and not self.pages[key].error:
            return epoch
        if not self.robots.allowed(url):
            log.debug("robots.txt disallows %s -- skipping", url)
            # Not a failure, and it must not count against --max-pages.
            self.frontier.refund()
            self._record_failure(key, url, depth, parent, ROBOTS_SKIP)
            return epoch

        page, epoch = self._fetch(driver, url, depth, epoch)

        if page is None or (page.error and not page.html):
            error = page.error if page is not None else "no response"
            log.warning("[d%d] FAILED %s (%s)", depth, url, error)
            self._record_failure(key, url, depth, parent, error)
            return epoch

        # Saving an interstitial as if it were the page would quietly poison the
        # mirror, so a wall we could not clear is recorded as a failure instead.
        if page.challenge is not None:
            self._record_failure(key, url, depth, parent, page.challenge.as_error)
            return epoch

        # Chrome happily "renders" PDFs and images; store those as assets instead.
        ctype = (page.content_type or "").split(";")[0].lower()
        if ctype and not any(ctype.startswith(t) for t in HTML_TYPES if t):
            log.info("[d%d] not HTML (%s) -> saving as asset: %s", depth, ctype, url)
            self.assets.fetch(url)
            self.frontier.mark_seen(key)
            return epoch

        host = urlsplit(url).hostname or ""
        self.assets.sync_cookies(page.cookies, host)
        self._remember_session(page.cookies, host)

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
        return epoch

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
        self._save_session()

        if not self.cfg.keep_raw:
            import shutil
            shutil.rmtree(self.cfg.raw_root, ignore_errors=True)

    # -- summary -----------------------------------------------------------
    def summary(self) -> Dict[str, object]:
        ok = [p for p in self.pages.values() if not p.error]
        blocked = [p for p in self.pages.values() if p.error == ROBOTS_SKIP]
        walled = [p for p in self.pages.values()
                  if p.error and p.error.startswith(CHALLENGE_PREFIX)]
        bad = [p for p in self.pages.values() if p.error and p.error != ROBOTS_SKIP]
        return {
            "pages": len(ok),
            "failed": len(bad),
            "blocked": len(blocked),
            "walled": len(walled),
            "walls_hit": self.walls_hit,
            "cleared": self.gate.solved,
            "throttled": self.throttle.snapshot(),
            "assets": self.assets.count,
            "asset_bytes": self.assets.total_bytes,
            "not_crawled": len(self.frontier.deferred),
            "max_depth_seen": max((p.depth for p in ok), default=0),
            "failures": [(p.url, p.error) for p in bad][:20],
        }
