"""URL canonicalisation, scope rules and URL -> local-file mapping."""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import threading
from typing import Dict, Optional, Set, Tuple
from urllib.parse import (parse_qsl, quote, unquote, urlencode, urljoin,
                          urlsplit, urlunsplit)

from .config import (ASSET_EXTENSIONS, ASSETS_DIR, PAGE_EXTENSIONS, PAGES_DIR,
                     TRACKING_PARAMS, Config)

DEFAULT_PORTS = {"http": 80, "https": 443}
SKIP_SCHEMES = {"javascript", "mailto", "tel", "sms", "data", "blob", "about",
                "file", "ftp", "magnet", "whatsapp", "intent", "market"}

_UNSAFE_CHARS = re.compile(r'[<>:"|?*\\/\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def _sha1(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


# --------------------------------------------------------------------------
# Canonicalisation
# --------------------------------------------------------------------------
def canonicalize(raw: str, base: Optional[str] = None,
                 keep_query: bool = True) -> Optional[Tuple[str, str]]:
    """Return ``(fetch_url, key)`` or ``None`` if the URL is not crawlable.

    ``fetch_url`` is what we actually request; ``key`` is a stable identity used
    for de-duplication (query parameters sorted, tracking params removed).
    """
    if not raw:
        return None
    raw = raw.strip().replace("\n", "").replace("\r", "").replace("\t", "")
    if not raw or raw.startswith("#"):
        return None

    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", raw)
    if scheme_match and scheme_match.group(1).lower() in SKIP_SCHEMES:
        return None

    try:
        absolute = urljoin(base, raw) if base else raw
        parts = urlsplit(absolute)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None

    host = parts.hostname.lower().rstrip(".")
    port = parts.port
    netloc = host if port in (None, DEFAULT_PORTS.get(parts.scheme)) else f"{host}:{port}"

    path = parts.path or "/"
    # Collapse ".." / "." and duplicate slashes without losing a trailing slash.
    trailing = path.endswith("/")
    path = posixpath.normpath(path)
    if path == ".":
        path = "/"
    if trailing and not path.endswith("/"):
        path += "/"
    if not path.startswith("/"):
        path = "/" + path
    path = quote(unquote(path), safe="/~:@!$&'()*+,;=%-._")

    pairs = parse_qsl(parts.query, keep_blank_values=True) if parts.query else []
    pairs = [(k, v) for k, v in pairs if not TRACKING_PARAMS.match(k)]
    if not keep_query:
        pairs = []
    query = urlencode(pairs, doseq=True)

    fetch_url = urlunsplit((parts.scheme, netloc, path, query, ""))
    key = urlunsplit((parts.scheme, netloc, path.rstrip("/") or "/",
                      urlencode(sorted(pairs), doseq=True), ""))
    return fetch_url, key


def url_key(raw: str, base: Optional[str] = None, keep_query: bool = True) -> Optional[str]:
    result = canonicalize(raw, base, keep_query)
    return result[1] if result else None


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a public-suffix dependency."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle the common two-level suffixes (co.uk, com.vn, com.au, ...).
    two_level = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go"}
    if len(parts) >= 3 and parts[-2] in two_level and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------
def host_in_scope(host: str, cfg: Config) -> bool:
    host = host.lower()
    if host in cfg.allowed_hosts:
        return True
    if cfg.include_subdomains:
        base = registrable_domain(cfg.start_host)
        if host == base or host.endswith("." + base):
            return True
        for extra in cfg.extra_domains:
            if host == extra or host.endswith("." + extra):
                return True
    return False


def in_scope(fetch_url: str, cfg: Config) -> bool:
    """Should this URL be crawled as a page of the mirror?"""
    parts = urlsplit(fetch_url)
    if not host_in_scope(parts.hostname or "", cfg):
        return False
    if cfg.path_prefix and not parts.path.startswith(cfg.path_prefix):
        return False
    if cfg.include_patterns and not any(p.search(fetch_url) for p in cfg.include_patterns):
        return False
    if any(p.search(fetch_url) for p in cfg.exclude_patterns):
        return False
    return True


def looks_like_page(fetch_url: str) -> bool:
    """True when the URL smells like HTML rather than a binary asset."""
    path = urlsplit(fetch_url).path
    ext = posixpath.splitext(path)[1].lower()
    if ext in PAGE_EXTENSIONS:
        return True
    if ext in ASSET_EXTENSIONS:
        return False
    # Unknown extension (e.g. /report.v2) -- assume a page, the fetch will tell us.
    return len(ext) > 5 or ext == ""


# --------------------------------------------------------------------------
# URL -> local path
# --------------------------------------------------------------------------
def sanitize_segment(segment: str, maxlen: int = 90) -> str:
    seg = unquote(segment)
    seg = _UNSAFE_CHARS.sub("_", seg)
    seg = seg.strip().rstrip(". ")
    if not seg:
        return "_"
    if seg.split(".")[0].upper() in _WIN_RESERVED:
        seg = "_" + seg
    if len(seg.encode("utf-8")) > maxlen:
        stem, ext = posixpath.splitext(seg)
        digest = _sha1(seg)
        keep = max(1, maxlen - len(ext) - 9)
        seg = stem.encode("utf-8")[:keep].decode("utf-8", "ignore") + "_" + digest + ext
    return seg


def _host_segment(parts) -> str:
    host = (parts.hostname or "unknown").lower()
    if parts.port and parts.port != DEFAULT_PORTS.get(parts.scheme):
        host = f"{host}_{parts.port}"
    return sanitize_segment(host)


def page_relpath(fetch_url: str) -> str:
    """Local path (posix, relative to the output root) for an HTML page."""
    parts = urlsplit(fetch_url)
    segments = [sanitize_segment(s) for s in parts.path.split("/") if s]

    if not segments or parts.path.endswith("/"):
        segments.append("index.html")
    else:
        last = segments[-1]
        stem, ext = posixpath.splitext(last)
        if ext and ext.lower() in PAGE_EXTENSIONS:
            segments[-1] = stem + ".html"
        elif not ext:
            # /about  ->  about/index.html so that relative links keep working.
            segments.append("index.html")
        else:
            segments[-1] = last + ".html"

    if parts.query:
        stem, ext = posixpath.splitext(segments[-1])
        segments[-1] = f"{stem}__q{_sha1(parts.query)}{ext}"

    return posixpath.join(PAGES_DIR, _host_segment(parts), *segments)


def asset_relpath(fetch_url: str, content_type: str = "") -> str:
    """Local path (posix, relative to the output root) for a binary/text asset."""
    parts = urlsplit(fetch_url)
    segments = [sanitize_segment(s) for s in parts.path.split("/") if s]
    if not segments:
        segments = ["index"]

    stem, ext = posixpath.splitext(segments[-1])
    if not ext:
        ext = _ext_from_content_type(content_type)
        segments[-1] = stem + ext
        stem, ext = posixpath.splitext(segments[-1])

    if parts.query:
        segments[-1] = f"{stem}__q{_sha1(parts.query)}{ext}"

    return posixpath.join(ASSETS_DIR, _host_segment(parts), *segments)


_CT_EXT = {
    "text/css": ".css", "text/javascript": ".js", "application/javascript": ".js",
    "application/x-javascript": ".js", "image/jpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
    "image/svg+xml": ".svg", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff", "font/woff2": ".woff2", "font/ttf": ".ttf",
    "font/otf": ".otf", "application/font-woff": ".woff",
    "application/json": ".json", "application/pdf": ".pdf",
    "text/plain": ".txt", "text/html": ".html", "video/mp4": ".mp4",
    "audio/mpeg": ".mp3", "image/bmp": ".bmp", "image/tiff": ".tiff",
}


def _ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    return _CT_EXT.get(ct, ".bin")


def relative_link(from_relpath: str, to_relpath: str) -> str:
    """Relative href from one local file to another (works over file://)."""
    from_dir = posixpath.dirname(from_relpath) or "."
    rel = posixpath.relpath(to_relpath, from_dir)
    return quote(rel, safe="/._-~()!$&'*+,;=@:")


class PathAllocator:
    """Hands out unique local paths and remembers who owns what.

    Two different URLs can naturally map onto the same file (``/about`` and
    ``/about/index.html``); the loser gets a short hash suffix.
    """

    def __init__(self) -> None:
        self._by_key: Dict[str, str] = {}
        self._taken: Dict[str, str] = {}   # relpath(lower) -> key
        self._lock = threading.Lock()

    def load(self, mapping: Dict[str, str]) -> None:
        with self._lock:
            for key, relpath in mapping.items():
                self._by_key[key] = relpath
                self._taken[relpath.lower()] = key

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._by_key.get(key)

    def allocate(self, key: str, preferred: str) -> str:
        with self._lock:
            existing = self._by_key.get(key)
            if existing:
                return existing

            candidate = preferred
            lowered = candidate.lower()
            if lowered in self._taken and self._taken[lowered] != key:
                stem, ext = posixpath.splitext(candidate)
                candidate = f"{stem}__{_sha1(key)}{ext}"
                lowered = candidate.lower()
                n = 1
                while lowered in self._taken and self._taken[lowered] != key:
                    candidate = f"{stem}__{_sha1(key)}_{n}{ext}"
                    lowered = candidate.lower()
                    n += 1

            self._by_key[key] = candidate
            self._taken[lowered] = key
            return candidate

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._by_key)
