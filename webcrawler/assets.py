"""Downloading CSS / JS / images / fonts and rewriting URLs inside CSS."""
from __future__ import annotations

import logging
import posixpath
import re
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Set
from urllib.parse import urlsplit

import requests

from .config import Config
from .urls import (asset_relpath, canonicalize, relative_link, sanitize_segment)

log = logging.getLogger("crawler.assets")

# url(...) in CSS, tolerating quotes and whitespace.
_CSS_URL = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<url>[^'")]+?)(?P=q)\s*\)""", re.I)
# @import "x.css" / @import url(x.css)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?(?P<q>['"]?)(?P<url>[^'")\s;]+)(?P=q)\s*\)?""", re.I)


@dataclass
class AssetRecord:
    url: str
    path: str
    content_type: str = ""
    size: int = 0
    error: Optional[str] = None


class AssetManager:
    """Thread-safe, de-duplicating asset downloader."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._records: Dict[str, AssetRecord] = {}     # key -> record
        self._lock = threading.Lock()
        self._inflight: Dict[str, threading.Event] = {}
        self._local = threading.local()

    # -- session -----------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": self.cfg.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })
            self._local.session = session
        return session

    def sync_cookies(self, cookies, origin_host: str) -> None:
        """Copy the browser's cookies into this thread's requests session."""
        for cookie in cookies or []:
            try:
                self.session.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=cookie.get("domain") or origin_host,
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue

    # -- state -------------------------------------------------------------
    def load(self, data: Dict[str, dict]) -> None:
        with self._lock:
            for key, rec in data.items():
                self._records[key] = AssetRecord(**rec)

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {k: vars(v) for k, v in self._records.items()}

    def path_map(self) -> Dict[str, str]:
        with self._lock:
            return {k: v.path for k, v in self._records.items() if v.error is None}

    @property
    def count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.error is None)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return sum(r.size for r in self._records.values())

    # -- download ----------------------------------------------------------
    def fetch(self, raw_url: str, base: Optional[str] = None, depth: int = 0) -> Optional[str]:
        """Download an asset if needed; return its output-root-relative path."""
        if not self.cfg.download_assets:
            return None
        canon = canonicalize(raw_url, base, keep_query=True)
        if not canon:
            return None
        fetch_url, key = canon

        with self._lock:
            record = self._records.get(key)
            if record is not None:
                return record.path if record.error is None else None
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                owner = True
            else:
                owner = False

        if not owner:
            event.wait(timeout=self.cfg.asset_timeout + 10)
            with self._lock:
                record = self._records.get(key)
            return record.path if record and record.error is None else None

        try:
            record = self._download(fetch_url, key, depth)
        except Exception as exc:  # never let one asset kill the crawl
            record = AssetRecord(url=fetch_url, path="", error=f"{exc.__class__.__name__}: {exc}")
        finally:
            with self._lock:
                self._records[key] = record
                self._inflight.pop(key, None)
            event.set()

        return record.path if record.error is None else None

    def _download(self, fetch_url: str, key: str, depth: int) -> AssetRecord:
        response = self.session.get(fetch_url, timeout=self.cfg.asset_timeout,
                                    stream=True, allow_redirects=True)
        try:
            if response.status_code >= 400:
                return AssetRecord(url=fetch_url, path="", error=f"HTTP {response.status_code}")

            content_type = response.headers.get("Content-Type", "")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > self.cfg.max_asset_bytes:
                return AssetRecord(url=fetch_url, path="", content_type=content_type,
                                   error=f"too large ({int(declared) // 1024} KB)")

            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > self.cfg.max_asset_bytes:
                    return AssetRecord(url=fetch_url, path="", content_type=content_type,
                                       error=f"too large (> {self.cfg.max_asset_mb} MB)")
            body = b"".join(chunks)
        finally:
            response.close()

        relpath = asset_relpath(fetch_url, content_type)
        target = self.cfg.output_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)

        is_css = relpath.lower().endswith(".css") or "text/css" in content_type.lower()
        if is_css and depth < self.cfg.css_depth:
            text = body.decode(response.encoding or "utf-8", "replace")
            text = self._rewrite_css(text, fetch_url, relpath, depth + 1)
            body = text.encode("utf-8")

        target.write_bytes(body)
        log.debug("asset %s -> %s (%d B)", fetch_url, relpath, len(body))
        return AssetRecord(url=fetch_url, path=relpath, content_type=content_type, size=len(body))

    # -- CSS ---------------------------------------------------------------
    def _rewrite_css(self, text: str, css_url: str, css_relpath: str, depth: int) -> str:
        """Download everything a stylesheet references and point it at local copies."""

        def resolve(raw: str) -> Optional[str]:
            """Local path if we got the file, absolute URL if we could not."""
            raw = raw.strip()
            if not raw or raw.startswith(("data:", "#", "about:")):
                return None
            local = self.fetch(raw, base=css_url, depth=depth)
            if local:
                return relative_link(css_relpath, local)
            # Download failed (404, too big, blocked) -- point at the live file so
            # the local copy behaves exactly like the original page.
            canon = canonicalize(raw, css_url, keep_query=True)
            return canon[0] if canon else None

        def sub_url(match: re.Match) -> str:
            target = resolve(match.group("url"))
            return f'url("{target}")' if target else match.group(0)

        def sub_import(match: re.Match) -> str:
            target = resolve(match.group("url"))
            return f'@import url("{target}")' if target else match.group(0)

        text = _CSS_IMPORT.sub(sub_import, text)
        text = _CSS_URL.sub(sub_url, text)
        return text

    def rewrite_inline_css(self, text: str, page_url: str, page_relpath: str) -> str:
        """Same idea, for <style> blocks and style="" attributes inside a page."""
        if not text or "url(" not in text.lower() and "@import" not in text.lower():
            return text

        def resolve(raw: str) -> Optional[str]:
            raw = raw.strip()
            if not raw or raw.startswith(("data:", "#", "about:")):
                return None
            local = self.fetch(raw, base=page_url, depth=1)
            if local:
                return relative_link(page_relpath, local)
            canon = canonicalize(raw, page_url, keep_query=True)
            return canon[0] if canon else None

        def sub_url(match: re.Match) -> str:
            local = resolve(match.group("url"))
            return f'url("{local}")' if local else match.group(0)

        def sub_import(match: re.Match) -> str:
            local = resolve(match.group("url"))
            return f'@import url("{local}")' if local else match.group(0)

        text = _CSS_IMPORT.sub(sub_import, text)
        return _CSS_URL.sub(sub_url, text)
