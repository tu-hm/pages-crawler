#!/usr/bin/env python3
"""
Recursive website crawler built on Selenium.

Renders every page with a real browser (so JavaScript-built markup is captured),
follows links depth-first through the whole site, downloads the CSS / JS / images
/ fonts each page needs, and rewrites all URLs so the result can be browsed
offline by double-clicking a file.

    python crawl.py https://example.com
    open output/example.com/index.html
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from webcrawler.config import DEFAULT_UA, Config
from webcrawler.crawler import Crawler
from webcrawler.urls import canonicalize, registrable_domain


class _Formatter(logging.Formatter):
    COLORS = {"WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[31m"}

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = self.COLORS.get(record.levelname)
        if color and sys.stderr.isatty():
            return f"{color}{message}\033[0m"
        return message


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=level, handlers=[handler])
    for noisy in ("selenium", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawl.py",
        description="Crawl a website recursively with Selenium and save a browsable offline copy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python crawl.py https://example.com
  python crawl.py https://docs.example.com --max-depth 4 --max-pages 500 --workers 6
  python crawl.py https://example.com/blog/ --path-prefix /blog/ --include-subdomains
  python crawl.py https://app.example.com --manual-login --no-headless
  python crawl.py https://example.com --resume            # continue an earlier crawl
""")

    p.add_argument("url", help="the URL to start from")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="output folder (default: ./output/<host>)")

    scope = p.add_argument_group("scope")
    scope.add_argument("-d", "--max-depth", type=int, default=3,
                       help="how many link hops from the start page (default: 3)")
    scope.add_argument("-n", "--max-pages", type=int, default=300,
                       help="stop after this many pages, 0 = unlimited (default: 300)")
    scope.add_argument("--include-subdomains", action="store_true",
                       help="also follow links to subdomains of the start host")
    scope.add_argument("--domain", action="append", default=[], metavar="HOST",
                       help="allow an extra host (repeatable)")
    scope.add_argument("--path-prefix", metavar="PATH",
                       help="only crawl URLs whose path starts with this (e.g. /docs/)")
    scope.add_argument("--include", action="append", default=[], metavar="REGEX",
                       help="only crawl URLs matching this regex (repeatable)")
    scope.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                       help="never crawl URLs matching this regex (repeatable)")
    scope.add_argument("--ignore-query", action="store_true",
                       help="treat ?a=1 and ?a=2 as the same page")
    scope.add_argument("--no-iframes", action="store_true",
                       help="do not follow iframe sources as pages")

    browser = p.add_argument_group("browser")
    browser.add_argument("-w", "--workers", type=int, default=4,
                         help="parallel browser instances (default: 4)")
    browser.add_argument("--browser", choices=("chrome", "firefox"), default="chrome")
    browser.add_argument("--no-headless", dest="headless", action="store_false",
                         help="show the browser window")
    browser.add_argument("--window-size", default="1440,1000")
    browser.add_argument("--user-agent", default=DEFAULT_UA)
    browser.add_argument("--timeout", type=int, default=30, metavar="SEC",
                         help="page load timeout (default: 30)")
    browser.add_argument("--wait", type=float, default=1.2, metavar="SEC",
                         help="settle time after a page loads (default: 1.2)")
    browser.add_argument("--no-scroll", dest="scroll", action="store_false",
                         help="skip the lazy-load scroll pass")
    browser.add_argument("--delay", type=float, default=0.4, metavar="SEC",
                         help="pause between requests per worker (default: 0.4)")
    browser.add_argument("--profile", metavar="DIR", dest="user_data_dir",
                         help="reuse a Chrome user-data directory (keeps you logged in)")
    browser.add_argument("--cookies", type=Path, metavar="FILE",
                         help="JSON file of cookies to load before crawling")
    browser.add_argument("--manual-login", action="store_true",
                         help="open the start page and wait for ENTER before crawling")

    assets = p.add_argument_group("assets")
    assets.add_argument("--no-assets", dest="download_assets", action="store_false",
                        help="save HTML only")
    assets.add_argument("--max-asset-mb", type=float, default=25.0,
                        help="skip assets bigger than this (default: 25)")
    assets.add_argument("--block-media", action="store_true",
                        help="stop the browser from streaming video/audio")

    misc = p.add_argument_group("other")
    misc.add_argument("--ignore-robots", dest="respect_robots", action="store_false",
                      help="do not honour robots.txt (only for sites you control)")
    misc.add_argument("--resume", action="store_true",
                      help="continue a previous crawl in the same output folder")
    misc.add_argument("--no-nav", dest="inject_nav", action="store_false",
                      help="do not add the little site-map badge to saved pages")
    misc.add_argument("--clean-raw", dest="keep_raw", action="store_false",
                      help="delete the raw HTML snapshots when finished")
    misc.add_argument("--rewrite-only", action="store_true",
                      help="skip crawling; just re-run the offline rewrite from raw snapshots")
    misc.add_argument("-v", "--verbose", action="store_true")
    misc.add_argument("-q", "--quiet", action="store_true")
    return p


def make_config(args: argparse.Namespace) -> Config:
    canon = canonicalize(args.url, keep_query=not args.ignore_query)
    if not canon:
        raise SystemExit(f"error: '{args.url}' is not an http(s) URL")
    start_url, _ = canon
    host = urlsplit(start_url).hostname or "site"

    output = args.output or Path("output") / host

    try:
        include = [re.compile(pattern) for pattern in args.include]
        exclude = [re.compile(pattern) for pattern in args.exclude]
    except re.error as exc:
        raise SystemExit(f"error: bad regex ({exc})")

    allowed = {host}
    allowed.update(d.lower().lstrip(".") for d in args.domain)

    cfg = Config(
        start_url=start_url,
        output_dir=output.resolve(),
        max_depth=max(0, args.max_depth),
        max_pages=max(0, args.max_pages),
        include_subdomains=args.include_subdomains,
        extra_domains={registrable_domain(d.lower().lstrip(".")) for d in args.domain},
        path_prefix=args.path_prefix,
        include_patterns=include,
        exclude_patterns=exclude,
        keep_query=not args.ignore_query,
        crawl_iframes=not args.no_iframes,
        workers=max(1, args.workers),
        headless=args.headless,
        browser=args.browser,
        window_size=args.window_size,
        user_agent=args.user_agent,
        page_timeout=args.timeout,
        render_wait=args.wait,
        scroll=args.scroll,
        delay=args.delay,
        user_data_dir=args.user_data_dir,
        cookies_file=args.cookies,
        manual_login=args.manual_login,
        block_media=args.block_media,
        download_assets=args.download_assets,
        max_asset_mb=args.max_asset_mb,
        respect_robots=args.respect_robots,
        resume=args.resume or args.rewrite_only,
        inject_nav=args.inject_nav,
        keep_raw=args.keep_raw,
        verbose=args.verbose,
    )
    cfg.start_host = host
    cfg.start_scheme = urlsplit(start_url).scheme
    cfg.allowed_hosts = allowed
    if args.manual_login:
        cfg.headless = False
        cfg.workers = 1
    return cfg


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    cfg = make_config(args)

    crawler = Crawler(cfg)
    started = time.time()

    if args.rewrite_only:
        if not crawler.load_manifest():
            raise SystemExit(f"error: no manifest in {cfg.output_dir} -- nothing to rewrite")
        canon = canonicalize(cfg.start_url, keep_query=cfg.keep_query)
        crawler.origin_key = crawler.origin_key or (canon[1] if canon else "")
    else:
        crawler.run()

    crawler.finalize()

    stats = crawler.summary()
    elapsed = time.time() - started
    index = cfg.output_dir / "index.html"

    print()
    print("=" * 66)
    print(f"  Pages saved      : {stats['pages']}")
    print(f"  Assets saved     : {stats['assets']}  ({stats['asset_bytes'] / 1048576:.1f} MB)")
    print(f"  Deepest level    : {stats['max_depth_seen']}")
    if stats["blocked"]:
        print(f"  Skipped (robots) : {stats['blocked']}")
    if stats["failed"]:
        print(f"  Failed           : {stats['failed']}")
        for url, error in stats["failures"][:5]:
            print(f"      - {url}  ({error})")
    if stats["not_crawled"]:
        print(f"  Left in queue    : {stats['not_crawled']}  (raise --max-depth / --max-pages, then --resume)")
    print(f"  Time             : {elapsed:.0f}s")
    print("-" * 66)
    print(f"  Open it:   open {index}")
    print(f"  Or serve:  python serve.py {cfg.output_dir}")
    print("=" * 66)
    return 0 if stats["pages"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
