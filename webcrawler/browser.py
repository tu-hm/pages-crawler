"""Selenium driver construction and page rendering."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from selenium import webdriver
from selenium.common.exceptions import (JavascriptException, TimeoutException,
                                        WebDriverException)
from selenium.webdriver.support.ui import WebDriverWait

from .config import Config

log = logging.getLogger("crawler.browser")


@dataclass
class RenderedPage:
    url: str                 # URL we asked for
    final_url: str           # URL after redirects / pushState
    html: str
    title: str = ""
    status: Optional[int] = None
    content_type: str = ""
    error: Optional[str] = None
    cookies: List[dict] = field(default_factory=list)


# Promote lazy-loading attributes into real ones, force-load native lazy images
# and expand <details>, so the saved HTML has everything the user would see.
_PREPARE_JS = r"""
const LAZY_SRC = ['data-src','data-original','data-lazy-src','data-echo','data-url','data-img'];
const LAZY_SET = ['data-srcset','data-lazy-srcset','data-lazyset'];
document.querySelectorAll('img,iframe,video,audio,source,embed').forEach(el => {
  for (const a of LAZY_SRC) {
    const v = el.getAttribute(a);
    if (v && !el.getAttribute('src')) { el.setAttribute('src', v); break; }
  }
  for (const a of LAZY_SET) {
    const v = el.getAttribute(a);
    if (v && !el.getAttribute('srcset')) { el.setAttribute('srcset', v); break; }
  }
  if (el.getAttribute('loading') === 'lazy') el.setAttribute('loading', 'eager');
});
document.querySelectorAll('details').forEach(d => d.open = true);
return document.querySelectorAll('img,iframe,video,source').length;
"""

_SCROLL_JS = r"""
const step = arguments[0], total = arguments[1];
window.scrollTo(0, Math.round(document.body.scrollHeight * (step / total)));
return document.body.scrollHeight;
"""


def build_driver(cfg: Config) -> webdriver.Remote:
    """Create a Chrome (or Firefox) driver. Selenium Manager finds the binary."""
    if cfg.browser == "firefox":
        return _build_firefox(cfg)
    return _build_chrome(cfg)


def _build_chrome(cfg: Config) -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    if cfg.headless:
        opts.add_argument("--headless=new")
    width, _, height = cfg.window_size.partition(",")
    opts.add_argument(f"--window-size={width},{height or '1000'}")
    opts.add_argument(f"--user-agent={cfg.user_agent}")
    for flag in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                 "--disable-extensions", "--disable-notifications",
                 "--disable-popup-blocking", "--mute-audio",
                 "--disable-background-networking", "--no-first-run",
                 "--no-default-browser-check", "--disable-features=Translate,MediaRouter",
                 "--log-level=3"):
        opts.add_argument(flag)
    if cfg.user_data_dir:
        opts.add_argument(f"--user-data-dir={cfg.user_data_dir}")

    prefs: Dict[str, object] = {
        "profile.default_content_setting_values.notifications": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    if cfg.block_media:
        # 2 == block.  Keeps huge videos from being fetched by the browser.
        prefs["profile.managed_default_content_settings.media_stream"] = 2
    opts.add_experimental_option("prefs", prefs)
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    # Performance log gives us real HTTP status codes.
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "SEVERE"})

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(cfg.page_timeout)
    driver.set_script_timeout(max(15, cfg.page_timeout))
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "deny"})
    except Exception:  # pragma: no cover - not fatal
        pass
    return driver


def _build_firefox(cfg: Config) -> webdriver.Firefox:
    opts = webdriver.FirefoxOptions()
    if cfg.headless:
        opts.add_argument("-headless")
    opts.set_preference("general.useragent.override", cfg.user_agent)
    opts.set_preference("dom.webnotifications.enabled", False)
    opts.set_preference("browser.download.folderList", 2)
    driver = webdriver.Firefox(options=opts)
    width, _, height = cfg.window_size.partition(",")
    driver.set_window_size(int(width), int(height or 1000))
    driver.set_page_load_timeout(cfg.page_timeout)
    driver.set_script_timeout(max(15, cfg.page_timeout))
    return driver


def apply_cookies(driver: webdriver.Remote, cookies: List[dict], origin: str) -> None:
    """Load cookies into the session (must be on the right origin first)."""
    if not cookies:
        return
    try:
        driver.get(origin)
    except WebDriverException:
        return
    for cookie in cookies:
        entry = {k: v for k, v in cookie.items()
                 if k in {"name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite"}}
        if "expiry" in entry:
            try:
                entry["expiry"] = int(entry["expiry"])
            except (TypeError, ValueError):
                entry.pop("expiry")
        try:
            driver.add_cookie(entry)
        except WebDriverException as exc:
            log.debug("cookie %s rejected: %s", entry.get("name"), exc)


def _status_from_logs(driver: webdriver.Remote, url: str,
                      final_url: str) -> tuple[Optional[int], str]:
    """Pull the *document's* HTTP status out of Chrome's performance log.

    Only ``type == "Document"`` responses count -- sub-resources like the
    favicon also show up in the log and would otherwise be mistaken for the
    page itself.
    """
    try:
        entries = driver.get_log("performance")
    except Exception:
        return None, ""

    documents: List[tuple[str, Optional[int], str]] = []
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, ValueError):
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        params = message.get("params", {})
        if params.get("type") != "Document":
            continue
        response = params.get("response", {})
        documents.append((response.get("url", ""),
                          response.get("status"),
                          response.get("mimeType", "") or ""))

    if not documents:
        return None, ""

    wanted = {_strip_fragment(url), _strip_fragment(final_url)}
    for doc_url, status, mime in reversed(documents):
        if _strip_fragment(doc_url) in wanted:
            return status, mime
    # Redirect chain we could not match exactly -- the last document is the page.
    return documents[-1][1], documents[-1][2]


def _strip_fragment(url: str) -> str:
    return (url or "").split("#", 1)[0]


def render(driver: webdriver.Remote, url: str, cfg: Config) -> RenderedPage:
    """Load a URL, wait for it to settle, and return the final DOM."""
    page = RenderedPage(url=url, final_url=url, html="")
    try:
        driver.get(url)
    except TimeoutException:
        page.error = "page load timeout"
    except WebDriverException as exc:
        page.error = f"navigation failed: {exc.__class__.__name__}: {str(exc)[:200]}"
        return page

    # 1. document.readyState
    try:
        WebDriverWait(driver, cfg.page_timeout).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
    except TimeoutException:
        page.error = page.error or "readyState timeout"

    if cfg.render_wait > 0:
        time.sleep(cfg.render_wait)

    # 2. Scroll through the page so lazy content loads.
    if cfg.scroll:
        try:
            for step in range(1, cfg.scroll_steps + 1):
                driver.execute_script(_SCROLL_JS, step, cfg.scroll_steps)
                time.sleep(0.18)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.25)
        except (JavascriptException, WebDriverException):
            pass

    # 3. Wait for the DOM to stop growing (JS frameworks, infinite lists).
    if cfg.stabilize:
        previous, stable = -1, 0
        deadline = time.time() + min(10.0, cfg.page_timeout)
        while time.time() < deadline and stable < 2:
            try:
                size = driver.execute_script("return document.documentElement.innerHTML.length")
            except WebDriverException:
                break
            stable = stable + 1 if size == previous else 0
            previous = size
            time.sleep(0.35)

    # 4. Normalise lazy attributes before snapshotting.
    try:
        driver.execute_script(_PREPARE_JS)
    except (JavascriptException, WebDriverException):
        pass

    try:
        page.html = driver.page_source or ""
        page.final_url = driver.current_url or url
        page.title = (driver.title or "").strip()
        page.cookies = driver.get_cookies()
    except WebDriverException as exc:
        page.error = page.error or f"snapshot failed: {exc.__class__.__name__}"
        return page

    if cfg.browser == "chrome":
        page.status, page.content_type = _status_from_logs(driver, url, page.final_url)

    if not page.html.strip():
        page.error = page.error or "empty document"
    return page
