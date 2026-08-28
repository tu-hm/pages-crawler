"""Parse rendered HTML: find links, pull down assets, rewrite for offline use."""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from bs4 import BeautifulSoup, Tag

from .assets import AssetManager
from .config import Config
from .urls import canonicalize, in_scope, looks_like_page, relative_link

log = logging.getLogger("crawler.rewriter")

# (tag, attribute) pairs that point at another *page*.
PAGE_ATTRS: Tuple[Tuple[str, str], ...] = (
    ("a", "href"), ("area", "href"), ("form", "action"),
)
# (tag, attribute) pairs that point at an *asset*.
ASSET_ATTRS: Tuple[Tuple[str, str], ...] = (
    ("img", "src"), ("script", "src"), ("source", "src"), ("video", "src"),
    ("video", "poster"), ("audio", "src"), ("embed", "src"), ("object", "data"),
    ("track", "src"), ("input", "src"), ("image", "href"), ("image", "xlink:href"),
    ("use", "href"), ("use", "xlink:href"),
)
SRCSET_ATTRS: Tuple[Tuple[str, str], ...] = (
    ("img", "srcset"), ("source", "srcset"),
)
# <link rel="..."> values whose href is an asset we want.
ASSET_RELS = {"stylesheet", "icon", "shortcut icon", "apple-touch-icon",
              "apple-touch-icon-precomposed", "mask-icon", "manifest",
              "preload", "prefetch", "modulepreload", "apple-touch-startup-image"}
# rels we leave pointing at the live site (they are metadata, not resources).
KEEP_ABSOLUTE_RELS = {"canonical", "alternate", "dns-prefetch", "preconnect",
                      "author", "license", "search", "pingback", "webmention"}

_META_REFRESH = re.compile(r"^\s*(\d+)\s*;\s*url\s*=\s*(.+?)\s*$", re.I)

NAV_MARKER = "__offline_crawler_nav__"


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _base_url(soup: BeautifulSoup, page_url: str) -> str:
    tag = soup.find("base", href=True)
    if tag:
        resolved = canonicalize(tag["href"], page_url, keep_query=True)
        if resolved:
            return resolved[0]
    return page_url


def _iter_url_attrs(soup: BeautifulSoup, kinds: Iterable[Tuple[str, str]]):
    for tag_name, attr in kinds:
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if isinstance(value, str) and value.strip():
                yield tag, attr, value.strip()


def _link_kind(tag: Tag) -> Optional[str]:
    """For <link>: 'asset', 'absolute' or None."""
    rels = {r.lower() for r in (tag.get("rel") or [])}
    if rels & ASSET_RELS:
        return "asset"
    if rels & KEEP_ABSOLUTE_RELS:
        return "absolute"
    return "asset" if tag.get("as") or tag.get("type") else "absolute"


def _parse_srcset(value: str) -> List[Tuple[str, str]]:
    """Split a srcset into [(url, descriptor)] preserving order."""
    entries = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        pieces = chunk.split(None, 1)
        entries.append((pieces[0], pieces[1] if len(pieces) > 1 else ""))
    return entries


# --------------------------------------------------------------------------
# Pass 1: discovery (runs while crawling)
# --------------------------------------------------------------------------
def extract_links(soup: BeautifulSoup, page_url: str, cfg: Config) -> List[str]:
    """Return canonical keys of in-scope pages linked from this document."""
    base = _base_url(soup, page_url)
    found: List[str] = []
    seen: Set[str] = set()

    candidates: List[str] = []
    for _tag, _attr, value in _iter_url_attrs(soup, PAGE_ATTRS):
        candidates.append(value)
    if cfg.crawl_iframes:
        for tag in soup.find_all(("iframe", "frame")):
            src = tag.get("src")
            if isinstance(src, str) and src.strip():
                candidates.append(src.strip())
    for tag in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
        match = _META_REFRESH.match(tag.get("content", "") or "")
        if match:
            candidates.append(match.group(2))

    for raw in candidates:
        canon = canonicalize(raw, base, cfg.keep_query)
        if not canon:
            continue
        fetch_url, key = canon
        if key in seen:
            continue
        if not in_scope(fetch_url, cfg) or not looks_like_page(fetch_url):
            continue
        seen.add(key)
        found.append(key)
    return found


def collect_assets(soup: BeautifulSoup, page_url: str, cfg: Config,
                   assets: AssetManager) -> int:
    """Download every asset this page references. Returns how many succeeded."""
    if not cfg.download_assets:
        return 0
    base = _base_url(soup, page_url)
    urls: List[str] = []

    for _tag, _attr, value in _iter_url_attrs(soup, ASSET_ATTRS):
        urls.append(value)
    for _tag, _attr, value in _iter_url_attrs(soup, SRCSET_ATTRS):
        urls.extend(url for url, _ in _parse_srcset(value))
    for tag in soup.find_all("link", href=True):
        if _link_kind(tag) == "asset":
            urls.append(tag["href"])
    for tag in soup.find_all("object", data=True):
        urls.append(tag["data"])

    ok = 0
    for raw in urls:
        raw = raw.strip()
        if not raw or raw.startswith(("data:", "blob:", "about:", "#")):
            continue
        if assets.fetch(raw, base=base):
            ok += 1

    # <style> blocks and style="" attributes are handled at rewrite time, but we
    # download their targets now so the finalise pass is purely local.
    for tag in soup.find_all("style"):
        if tag.string:
            assets.rewrite_inline_css(tag.string, base, "pages/_probe/_.html")
    for tag in soup.find_all(style=True):
        assets.rewrite_inline_css(tag["style"], base, "pages/_probe/_.html")
    return ok


# --------------------------------------------------------------------------
# Pass 2: rewriting (runs after the crawl, when every path is known)
# --------------------------------------------------------------------------
def rewrite_page(html: str, page_url: str, page_path: str, cfg: Config,
                 page_map: Dict[str, str], asset_map: Dict[str, str],
                 sitemap_path: str = "index.html") -> str:
    """Rewrite one document so every known resource resolves on the filesystem."""
    soup = make_soup(html)
    base = _base_url(soup, page_url)

    def local_page(raw: str) -> Optional[str]:
        canon = canonicalize(raw, base, cfg.keep_query)
        if not canon:
            return None
        target = page_map.get(canon[1])
        return relative_link(page_path, target) if target else None

    def local_asset(raw: str) -> Optional[str]:
        canon = canonicalize(raw, base, keep_query=True)
        if not canon:
            return None
        target = asset_map.get(canon[1])
        return relative_link(page_path, target) if target else None

    def absolutise(raw: str) -> Optional[str]:
        canon = canonicalize(raw, base, cfg.keep_query)
        return canon[0] if canon else None

    # <base> would break every relative path we are about to write.
    for tag in soup.find_all("base"):
        tag.decompose()

    # Page links: local copy if we have it, otherwise the live URL.
    for tag, attr, value in list(_iter_url_attrs(soup, PAGE_ATTRS)):
        if value.startswith("#") or value.lower().startswith(("javascript:", "mailto:", "tel:", "sms:", "data:")):
            continue
        fragment = ""
        target = value
        if "#" in value and not value.startswith("#"):
            target, _, fragment = value.partition("#")
            fragment = "#" + fragment
        local = local_page(target)
        if local:
            tag[attr] = local + fragment
            tag["data-offline"] = "local"
        else:
            absolute = absolutise(target)
            if absolute:
                tag[attr] = absolute + fragment
                tag["data-offline"] = "remote"
                if tag.name == "a":
                    tag["target"] = tag.get("target") or "_blank"
                    rels = tag.get("rel") or []
                    if isinstance(rels, str):
                        rels = rels.split()
                    if "noopener" not in rels:
                        rels.append("noopener")
                    tag["rel"] = " ".join(rels)

    # iframes/frames: a crawled page if possible, else an asset, else live.
    for tag in soup.find_all(("iframe", "frame")):
        src = (tag.get("src") or "").strip()
        if not src or src.startswith(("about:", "data:", "javascript:")):
            continue
        tag["src"] = local_page(src) or local_asset(src) or absolutise(src) or src

    # Assets.
    for tag, attr, value in list(_iter_url_attrs(soup, ASSET_ATTRS)):
        if value.startswith(("data:", "blob:", "#")):
            continue
        local = local_asset(value)
        tag[attr] = local if local else (absolutise(value) or value)

    for tag, attr, value in list(_iter_url_attrs(soup, SRCSET_ATTRS)):
        rewritten = []
        for url, descriptor in _parse_srcset(value):
            if url.startswith(("data:", "blob:")):
                new = url
            else:
                new = local_asset(url) or absolutise(url) or url
            rewritten.append(f"{new} {descriptor}".strip())
        tag[attr] = ", ".join(rewritten)

    for tag in soup.find_all("link", href=True):
        kind = _link_kind(tag)
        href = tag["href"].strip()
        if kind == "asset" and not href.startswith(("data:", "#")):
            local = local_asset(href)
            tag["href"] = local if local else (absolutise(href) or href)
        elif kind == "absolute":
            tag["href"] = absolutise(href) or href

    # Subresource integrity hashes fail once the file is served from disk.
    for tag in soup.find_all(["script", "link"]):
        tag.attrs.pop("integrity", None)
        tag.attrs.pop("crossorigin", None)

    # Inline CSS.
    for tag in soup.find_all("style"):
        if tag.string:
            tag.string.replace_with(_rewrite_css_local(tag.string, base, page_path, asset_map))
    for tag in soup.find_all(style=True):
        tag["style"] = _rewrite_css_local(tag["style"], base, page_path, asset_map)

    # meta refresh
    for tag in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
        match = _META_REFRESH.match(tag.get("content", "") or "")
        if match:
            target = local_page(match.group(2)) or absolutise(match.group(2))
            if target:
                tag["content"] = f"{match.group(1)}; url={target}"

    _ensure_charset(soup)
    if cfg.inject_nav:
        _inject_nav(soup, page_path, page_url, sitemap_path)
    return str(soup)


_CSS_URL = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<url>[^'")]+?)(?P=q)\s*\)""", re.I)


def _rewrite_css_local(text: str, base: str, page_path: str,
                       asset_map: Dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        raw = match.group("url").strip()
        if raw.startswith(("data:", "#", "about:")):
            return match.group(0)
        canon = canonicalize(raw, base, keep_query=True)
        if not canon:
            return match.group(0)
        target = asset_map.get(canon[1])
        if not target:
            return f'url("{canon[0]}")'
        return f'url("{relative_link(page_path, target)}")'

    return _CSS_URL.sub(sub, text)


def _ensure_charset(soup: BeautifulSoup) -> None:
    head = soup.find("head")
    if head is None:
        return
    for meta in head.find_all("meta"):
        if meta.get("charset") or (meta.get("http-equiv", "").lower() == "content-type"):
            meta["charset"] = "utf-8"
            meta.attrs.pop("http-equiv", None)
            meta.attrs.pop("content", None)
            return
    tag = soup.new_tag("meta")
    tag["charset"] = "utf-8"
    head.insert(0, tag)


_NAV_CSS = (
    "position:fixed;right:12px;bottom:12px;z-index:2147483647;"
    "font:12px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
    "background:rgba(17,17,20,.88);color:#fff;border-radius:8px;padding:7px 11px;"
    "box-shadow:0 2px 12px rgba(0,0,0,.3);backdrop-filter:blur(6px);"
    "display:flex;gap:10px;align-items:center;max-width:70vw;"
)


def _inject_nav(soup: BeautifulSoup, page_path: str, page_url: str,
                sitemap_path: str) -> None:
    body = soup.find("body")
    if body is None or soup.find(id=NAV_MARKER):
        return
    box = soup.new_tag("div", id=NAV_MARKER)
    box["style"] = _NAV_CSS
    box["data-html2canvas-ignore"] = "true"

    sitemap_link = soup.new_tag("a", href=relative_link(page_path, sitemap_path))
    sitemap_link.string = "▤ Site map"
    sitemap_link["style"] = "color:#8ab4ff;text-decoration:none;font-weight:600;white-space:nowrap"

    live_link = soup.new_tag("a", href=page_url, target="_blank", rel="noopener")
    live_link.string = "↗ Live page"
    live_link["style"] = "color:#9aa0a6;text-decoration:none;white-space:nowrap"

    close = soup.new_tag("span")
    close.string = "✕"
    close["onclick"] = "this.parentNode.remove()"
    close["style"] = "cursor:pointer;color:#9aa0a6;padding-left:2px"

    box.append(sitemap_link)
    box.append(live_link)
    box.append(close)
    body.append(box)
