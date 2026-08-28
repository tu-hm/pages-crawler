"""Crawler configuration and shared constants."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

# Extensions that we treat as "a page" (something we render with the browser and
# recurse into).  The empty string covers extension-less URLs such as /about.
PAGE_EXTENSIONS: Set[str] = {
    "", ".html", ".htm", ".xhtml", ".shtml", ".php", ".php3", ".php4", ".php5",
    ".asp", ".aspx", ".jsp", ".jspx", ".cgi", ".pl", ".do", ".action", ".cfm",
}

# Everything below is downloaded verbatim instead of being rendered.
ASSET_EXTENSIONS: Set[str] = {
    # styles / scripts
    ".css", ".js", ".mjs", ".cjs", ".map",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp", ".tiff",
    # fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # media
    ".mp4", ".webm", ".ogg", ".ogv", ".mp3", ".wav", ".m4a", ".mov", ".avi",
    # documents / data
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".csv", ".txt", ".json", ".xml", ".rss", ".atom", ".webmanifest",
}

# Query parameters that only exist for analytics -- dropping them avoids
# crawling the same page a dozen times.
TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|gclsrc|dclid|msclkid|mc_[a-z]+|_ga|_gl|"
    r"igshid|yclid|ref_src|ref_url|spm|scm|vero_id|s_kwcid|ttclid|twclid)$",
    re.I,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Directory names inside the output folder.
PAGES_DIR = "pages"
ASSETS_DIR = "assets"
META_DIR = "_crawl"
RAW_DIR = "_crawl/raw"
MANIFEST = "_crawl/manifest.json"


@dataclass
class Config:
    """Everything the crawler needs to know, in one place."""

    start_url: str
    output_dir: Path

    # --- scope -------------------------------------------------------------
    max_depth: int = 3
    max_pages: int = 300
    include_subdomains: bool = False
    extra_domains: Set[str] = field(default_factory=set)
    path_prefix: Optional[str] = None
    include_patterns: List[re.Pattern] = field(default_factory=list)
    exclude_patterns: List[re.Pattern] = field(default_factory=list)
    keep_query: bool = True
    crawl_iframes: bool = True

    # --- browser -----------------------------------------------------------
    workers: int = 4
    headless: bool = True
    browser: str = "chrome"          # chrome | firefox
    window_size: str = "1440,1000"
    user_agent: str = DEFAULT_UA
    page_timeout: int = 30
    render_wait: float = 1.2         # extra settle time after readyState=complete
    stabilize: bool = True           # wait until the DOM stops changing
    scroll: bool = True              # scroll to trigger lazy-loading
    scroll_steps: int = 8
    delay: float = 0.4               # politeness delay between requests per worker
    user_data_dir: Optional[str] = None
    cookies_file: Optional[Path] = None
    save_cookies: Optional[Path] = None   # write the session back out when done
    manual_login: bool = False
    block_media: bool = False        # don't let the browser fetch video/audio

    # --- anti-bot walls ----------------------------------------------------
    # When a page turns out to be a CAPTCHA / interstitial rather than content,
    # open a window and let *you* clear it, then reuse that session.  Nothing
    # here solves a challenge automatically.
    solve_challenges: bool = True
    challenge_wait: float = 12.0     # grace period for a self-clearing check
    max_retries: int = 2             # retries for rate limits / transient errors
    backoff: float = 5.0             # first backoff pause, doubles each retry

    # --- assets ------------------------------------------------------------
    download_assets: bool = True
    max_asset_mb: float = 25.0
    css_depth: int = 4               # how deep to follow @import / url() chains
    asset_timeout: int = 30

    # --- behaviour ---------------------------------------------------------
    respect_robots: bool = True
    from_sitemap: bool = True        # seed the queue from /sitemap.xml when present
    resume: bool = False
    inject_nav: bool = True
    keep_raw: bool = True
    verbose: bool = False

    # --- derived (filled in by __post_init__) ------------------------------
    start_host: str = ""
    start_scheme: str = "https"
    allowed_hosts: Set[str] = field(default_factory=set)

    # Paths ------------------------------------------------------------------
    @property
    def pages_root(self) -> Path:
        return self.output_dir / PAGES_DIR

    @property
    def assets_root(self) -> Path:
        return self.output_dir / ASSETS_DIR

    @property
    def meta_root(self) -> Path:
        return self.output_dir / META_DIR

    @property
    def raw_root(self) -> Path:
        return self.output_dir / RAW_DIR

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / MANIFEST

    @property
    def max_asset_bytes(self) -> int:
        return int(self.max_asset_mb * 1024 * 1024)

    def ensure_dirs(self) -> None:
        for d in (self.output_dir, self.pages_root, self.assets_root,
                  self.meta_root, self.raw_root):
            d.mkdir(parents=True, exist_ok=True)
