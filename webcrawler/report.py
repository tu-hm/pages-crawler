"""Generates index.html -- the offline site map you actually browse from."""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import unquote, urlsplit

from .config import Config
from .urls import relative_link

log = logging.getLogger("crawler.report")


class Node:
    __slots__ = ("name", "children", "pages")

    def __init__(self, name: str):
        self.name = name
        self.children: Dict[str, "Node"] = {}
        self.pages: List[object] = []

    def child(self, name: str) -> "Node":
        node = self.children.get(name)
        if node is None:
            node = Node(name)
            self.children[name] = node
        return node

    def count(self) -> int:
        return len(self.pages) + sum(c.count() for c in self.children.values())


def _build_tree(pages) -> Node:
    root = Node("")
    for record in sorted(pages, key=lambda p: (p.depth, p.url)):
        parts = urlsplit(record.final_url or record.url)
        host = parts.hostname or "unknown"
        node = root.child(host)
        # Decode %C3%ADnh -> ính so non-ASCII slugs stay readable in the tree.
        for segment in [s for s in parts.path.split("/") if s]:
            node = node.child(unquote(segment))
        node.pages.append(record)
    return root


def _human(n: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PB"


def _status_class(status: Optional[int]) -> str:
    if status is None:
        return "s-unknown"
    if status < 300:
        return "s-ok"
    if status < 400:
        return "s-redir"
    return "s-err"


def _collapse(node: Node) -> tuple[str, Node]:
    """Merge single-child chains so /hc/vi-vn/articles is one row, not three."""
    label = node.name
    while len(node.children) == 1 and not node.pages:
        (only,) = node.children.values()
        label = f"{label}/{only.name}" if label else only.name
        node = only
    return label, node


def _render_node(node: Node, depth: int = 0) -> str:
    out: List[str] = []
    for record in node.pages:
        href = html.escape(relative_link("index.html", record.path))
        title = html.escape(record.title or record.url)
        url = html.escape(record.final_url or record.url)
        status = record.status if record.status is not None else "—"
        out.append(
            f'<li class="page" data-search="{html.escape((record.title or "") + " " + record.url).lower()}">'
            f'<a class="pg" href="{href}" title="{url}">'
            f'<span class="t">{title}</span>'
            f'<span class="u">{url}</span></a>'
            f'<span class="meta"><span class="badge {_status_class(record.status)}">{status}</span>'
            f'<span class="badge depth">d{record.depth}</span></span></li>'
        )

    for name in sorted(node.children):
        label, child = _collapse(node.children[name])
        total = child.count()
        if total == 0:
            continue
        body = _render_node(child, depth + 1)
        # Open the upper levels, and any branch small enough to scan at a glance.
        open_attr = " open" if (depth < 2 or total <= 25) else ""
        out.append(
            f'<li class="dir"><details{open_attr}><summary>'
            f'<span class="dirname">{html.escape(label)}</span>'
            f'<span class="cnt">{total}</span></summary>'
            f'<ul>{body}</ul></details></li>'
        )
    return "".join(out)


_CSS = """
:root{--bg:#fbfbfd;--fg:#1a1a1f;--muted:#6b7280;--line:#e4e4e9;--card:#fff;
      --accent:#2563eb;--ok:#0f9960;--err:#d1343c;--redir:#b45309;--chip:#f0f0f5}
@media (prefers-color-scheme:dark){
  :root{--bg:#111114;--fg:#e8e8ec;--muted:#9295a1;--line:#2a2a32;--card:#18181d;
        --accent:#7aa2ff;--ok:#3ecf8e;--err:#ff6b6b;--redir:#e0a35c;--chip:#22222a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
header h1{font-size:20px;margin:0 0 4px}
header .sub{color:var(--muted);font-size:13px;word-break:break-all}
header .sub a{color:var(--accent)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:9px 14px;min-width:96px}
.stat b{display:block;font-size:19px;line-height:1.2}
.stat span{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.toolbar{display:flex;gap:8px;align-items:center;margin:14px 0;flex-wrap:wrap}
#q{flex:1;min-width:220px;padding:9px 12px;border:1px solid var(--line);border-radius:9px;
   background:var(--card);color:var(--fg);font-size:14px}
button{padding:8px 13px;border:1px solid var(--line);border-radius:9px;background:var(--card);
       color:var(--fg);cursor:pointer;font-size:13px}
button:hover{border-color:var(--accent);color:var(--accent)}
.start{display:inline-block;margin:2px 0 6px;padding:9px 16px;background:var(--accent);color:#fff;
       border-radius:9px;text-decoration:none;font-weight:600}
ul{list-style:none;margin:0;padding-left:16px}
.tree>ul{padding-left:0}
li.dir>details>summary{cursor:pointer;padding:4px 6px;border-radius:7px;
       display:flex;align-items:center;gap:8px;user-select:none}
li.dir>details>summary:hover{background:var(--chip)}
.dirname{font-weight:600}
.cnt{background:var(--chip);color:var(--muted);border-radius:20px;padding:0 8px;font-size:11px}
li.page{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:7px}
li.page:hover{background:var(--chip)}
a.pg{flex:1;min-width:0;text-decoration:none;color:var(--fg);display:flex;
     flex-direction:column;line-height:1.35}
a.pg:hover .t{color:var(--accent);text-decoration:underline}
a.pg .t{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
a.pg .u{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{display:flex;gap:5px;flex-shrink:0}
.badge{font-size:10.5px;padding:1px 7px;border-radius:20px;background:var(--chip);color:var(--muted)}
.s-ok{color:var(--ok)}.s-err{color:var(--err)}.s-redir{color:var(--redir)}
section{margin-top:26px}
section h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
           margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:6px 10px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td{padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-all}
tr:last-child td{border-bottom:none}
td.reason{color:var(--err);white-space:nowrap;width:1%}
.hidden{display:none!important}
footer{margin-top:34px;color:var(--muted);font-size:12px}
"""

_JS = """
const q=document.getElementById('q');
const tree=document.querySelector('.tree');
function filter(){
  const term=q.value.trim().toLowerCase();
  tree.querySelectorAll('li.page').forEach(li=>{
    li.classList.toggle('hidden', term && !li.dataset.search.includes(term));
  });
  tree.querySelectorAll('li.dir').forEach(li=>{
    const visible=li.querySelectorAll('li.page:not(.hidden)').length;
    li.classList.toggle('hidden', term && visible===0);
    if(term && visible) li.querySelector('details').open=true;
  });
  document.getElementById('hits').textContent =
    term ? tree.querySelectorAll('li.page:not(.hidden)').length+' match' : '';
}
q.addEventListener('input',filter);
document.getElementById('expand').onclick=()=>tree.querySelectorAll('details').forEach(d=>d.open=true);
document.getElementById('collapse').onclick=()=>tree.querySelectorAll('details').forEach(d=>d.open=false);
document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}});
"""


def write_sitemap(cfg: Config, pages: Dict[str, object], assets,
                  not_crawled: Dict[str, int], start_key: str,
                  started_at: datetime) -> None:
    ok = [p for p in pages.values() if not p.error and p.path]
    blocked = [p for p in pages.values() if p.error == "blocked by robots.txt"]
    failed = [p for p in pages.values() if p.error and p.error != "blocked by robots.txt"]
    tree = _build_tree(ok)

    start = pages.get(start_key)
    start_href = relative_link("index.html", start.path) if start and start.path else ""
    finished = datetime.now(timezone.utc)
    duration = finished - started_at

    stats = [
        (len(ok), "pages"),
        (assets.count, "assets"),
        (_human(assets.total_bytes), "downloaded"),
        (max((p.depth for p in ok), default=0), "max depth"),
        (len(failed), "failed"),
        (len(blocked), "robots-skipped"),
        (len(not_crawled), "not fetched"),
    ]
    stat_html = "".join(
        f'<div class="stat"><b>{html.escape(str(value))}</b><span>{label}</span></div>'
        for value, label in stats
    )

    failed_rows = "".join(
        f'<tr><td>{html.escape(p.url)}</td><td class="reason">{html.escape(p.error or "")}</td></tr>'
        for p in sorted(failed, key=lambda p: p.url)[:400]
    )
    pending_rows = "".join(
        f'<tr><td><a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(url)}</a></td>'
        f'<td class="reason">depth {depth}</td></tr>'
        for url, depth in sorted(not_crawled.items())[:400]
    )

    sections = ""
    if failed_rows:
        sections += (f'<section><h2>Failed ({len(failed)})</h2>'
                     f'<div class="card"><table>{failed_rows}</table></div></section>')
    if pending_rows:
        sections += (f'<section><h2>Discovered but not downloaded ({len(not_crawled)})</h2>'
                     f'<div class="card"><p style="color:var(--muted);font-size:12px;margin:6px 4px">'
                     f'Beyond --max-depth or --max-pages. Raise the limits and re-run with '
                     f'<code>--resume</code> to pick these up.</p>'
                     f'<table>{pending_rows}</table></div></section>')

    doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline mirror — {html.escape(cfg.start_host)}</title>
<style>{_CSS}</style>
</head><body><div class="wrap">
<header>
  <h1>Offline mirror of {html.escape(cfg.start_host)}</h1>
  <div class="sub">Crawled from <a href="{html.escape(cfg.start_url)}" target="_blank" rel="noopener">{html.escape(cfg.start_url)}</a>
   · {finished.astimezone().strftime('%Y-%m-%d %H:%M')} · took {int(duration.total_seconds())}s</div>
</header>
<div class="stats">{stat_html}</div>
{'<a class="start" href="' + html.escape(start_href) + '">Open the start page &rarr;</a>' if start_href else ''}
<div class="toolbar">
  <input id="q" type="search" placeholder="Filter pages by title or URL  (press / to focus)" autocomplete="off">
  <span id="hits" style="color:var(--muted);font-size:12px"></span>
  <button id="expand">Expand all</button>
  <button id="collapse">Collapse all</button>
</div>
<div class="tree"><ul>{_render_node(tree)}</ul></div>
{sections}
<footer>Every page below is a real file on disk. Links to pages that were crawled stay local;
anything else still points at the live site.</footer>
</div><script>{_JS}</script></body></html>
"""
    (cfg.output_dir / "index.html").write_text(doc, "utf-8")
    log.info("site map written to %s", cfg.output_dir / "index.html")
