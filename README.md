# crawler-pages

Recursive website crawler built on **Selenium + Python**. Give it one URL; it renders
every page with a real browser, follows links through the whole site (children,
children-of-children, as deep as you allow), downloads the CSS / JS / images / fonts
each page needs, and rewrites every URL so you can **browse the whole thing offline
by double-clicking a file**.

Because pages go through a real browser, JavaScript-rendered content is captured —
things `requests` + BeautifulSoup would never see.

---

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

You need Chrome (or Firefox) installed. The matching driver is downloaded
automatically by Selenium Manager — nothing else to set up.

## Use

```bash
python crawl.py https://example.com
```

Then open the site map it prints at the end:

```bash
open output/example.com/index.html
```

That page lists every captured URL as a collapsible tree with a search box. Click any
entry to open the real saved page; links inside saved pages go to your local copies,
so you can keep clicking down through children and grandchildren without a network.

### Typical runs

```bash
# go deeper and wider, with more browsers in parallel
python crawl.py https://docs.example.com --max-depth 5 --max-pages 1000 --workers 6

# only one section of a site
python crawl.py https://example.com/blog/ --path-prefix /blog/

# include subdomains too
python crawl.py https://example.com --include-subdomains

# a site that needs a login: a window opens, you sign in, press ENTER
python crawl.py https://app.example.com --manual-login

# a site behind a CAPTCHA / "checking your browser" screen:
# it pauses, you clear the check once, and the session is saved for next time
python crawl.py https://example.com --workers 1 --save-cookies session.json
python crawl.py https://example.com --cookies session.json --resume

# continue where a previous run stopped (same output folder)
python crawl.py https://example.com --max-pages 2000 --resume

# HTML only, no images/CSS/JS
python crawl.py https://example.com --no-assets
```

Some pages behave better over `http://` than `file://` (service workers, strict
`fetch` code). If one looks off, serve the folder:

```bash
python serve.py output/example.com
```

---

## When a site blocks you

Some sites answer with a CAPTCHA, a Cloudflare "Checking your browser..." screen,
or a rate-limit page instead of content. The crawler **recognises** these and
refuses to save them — otherwise your mirror quietly fills up with
`Just a moment...` pages that look fine in the file listing and are worthless.

What it does *not* do is defeat them. There is no CAPTCHA solver here, no
fingerprint spoofing, no proxy rotation. When it hits a wall it stops and opens
a real browser window so **you** can clear the check the way any other visitor
would, then reuses that session for the rest of the crawl:

```
======================================================================
  CLOUDFLARE INTERSTITIAL — the site wants a human
  https://example.com/pricing

  A browser window is open. Complete the check there exactly as
  you would when visiting normally, and wait for the real page.

  Then: ENTER to carry on   ·   's' to skip walled pages this run
======================================================================
```

Checks that clear themselves (most Cloudflare JS challenges do) are waited out
for `--challenge-wait` seconds and never bother you at all. Only one window
opens no matter how many workers are running.

Save what you earn, and later runs start already past the wall:

```bash
python crawl.py https://example.com --save-cookies session.json
python crawl.py https://example.com --cookies session.json --resume
```

Cloudflare's `cf_clearance` is tied to your IP and user agent, so keep both the
same between runs and it keeps working — typically for a few hours.

### Blocked anyway? Try these, in order

1. **Slow down.** Four workers at `--delay 0.4` is ~10 requests/second, which a
   lot of sites treat as an attack. `--workers 1 --delay 3` fixes more "Cloudflare
   is blocking me" problems than anything else. The crawler now backs off on its
   own after a `429`, doubling the gap per host, but starting polite is better.
2. **Reuse a session** with `--save-cookies` / `--cookies` as above.
3. **If the site is yours**, stop fighting your own WAF — allowlist the crawler in
   Cloudflare instead. Add a WAF custom rule with action *Skip*, matching either
   your IP (`ip.src eq 203.0.113.7`) or a secret header you send. That is the
   supported way in, and it survives everything a bypass trick would not.
4. **Look for the front door.** A public API, a data export, an RSS feed or the
   `sitemap.xml` often gets you the same content with none of this.

Walls that a person at your IP genuinely cannot clear — Cloudflare error `1020`
(a firewall rule refused you) or `1015` (rate limited) — are recorded as failures
rather than prompting you, because there is nothing for you to click.

`--no-solve` turns the prompting off entirely: walled pages are just logged as
blocked and the crawl runs unattended.

---

## What you get

```
output/example.com/
├── index.html              ← START HERE: searchable site map of everything captured
├── pages/
│   └── example.com/        ← the pages, mirroring the site's own URL structure
│       ├── index.html
│       └── about/index.html
├── assets/
│   └── example.com/        ← css, js, images, fonts (shared across all pages)
└── _crawl/
    ├── manifest.json       ← every URL, local path, status, depth, parent, links
    └── raw/                ← untouched HTML snapshots (used by --resume)
```

Every saved page carries a small badge in the bottom-right corner linking back to the
site map and out to the live page. Turn it off with `--no-nav`.

---

## Options

Run `python crawl.py --help` for the full list.

**Scope**

| Flag | Meaning |
| --- | --- |
| `-d, --max-depth N` | link hops from the start page (default 3) |
| `-n, --max-pages N` | stop after N pages, `0` = unlimited (default 300) |
| `--include-subdomains` | also follow `*.example.com` |
| `--domain HOST` | allow one more host (repeatable) |
| `--path-prefix /docs/` | only URLs under this path |
| `--include REGEX` / `--exclude REGEX` | fine-grained filters (repeatable) |
| `--ignore-query` | treat `?page=1` and `?page=2` as one page |
| `--no-iframes` | don't follow iframe sources |

**Browser**

| Flag | Meaning |
| --- | --- |
| `-w, --workers N` | parallel browser instances (default 4) |
| `--no-headless` | watch it work |
| `--wait SEC` | settle time after load, raise for slow SPAs (default 1.2) |
| `--no-scroll` | skip the lazy-load scroll pass |
| `--delay SEC` | pause between requests per worker (default 0.4) |
| `--timeout SEC` | page load timeout (default 30) |
| `--profile DIR` | reuse a Chrome profile — keeps you logged in |
| `--cookies FILE` | load cookies from a JSON file |
| `--manual-login` | sign in by hand, then crawl with that session |
| `--save-cookies FILE` | write the session out when done, to reuse with `--cookies` |
| `--browser firefox` | use Firefox instead |

**Anti-bot walls** — see [When a site blocks you](#when-a-site-blocks-you)

| Flag | Meaning |
| --- | --- |
| `--no-solve` | never prompt; record walled pages as blocked and keep going |
| `--challenge-wait SEC` | let a check clear itself this long before asking you (default 12) |
| `--retries N` | retries when a host rate-limits us (default 2) |
| `--backoff SEC` | first pause after a rate limit, doubles each retry (default 5) |

**Assets & behaviour**

| Flag | Meaning |
| --- | --- |
| `--no-assets` | HTML only |
| `--max-asset-mb N` | skip files bigger than this (default 25) |
| `--block-media` | stop the browser streaming video/audio |
| `--ignore-robots` | ignore `robots.txt` (only for sites you control) |
| `--no-sitemap` | don't seed the queue from the site's `sitemap.xml` |
| `--resume` | continue a previous crawl |
| `--rewrite-only` | redo the offline rewrite from snapshots, no crawling |
| `--clean-raw` | delete raw snapshots when done (breaks `--resume`) |
| `--no-nav` | no site-map badge on saved pages |

---

## How it works

0. **Seed** — before following any links, the crawler reads `robots.txt` and
   `sitemap.xml` and queues whatever the site publishes about itself. That is the
   site telling you what it wants crawled: better coverage than guessing from
   links, and far fewer requests to discover it. `--no-sitemap` skips it.
1. **Frontier** — a depth-ordered queue, deduplicated by a canonical form of each URL
   (default ports dropped, fragments removed, tracking params like `utm_*` stripped,
   query keys sorted). Workers share it and the crawl ends only when the queue is
   empty *and* nobody is still working.
2. **Render** — each worker drives its own browser: load, wait for `readyState`,
   scroll top-to-bottom so lazy images fire, wait for the DOM to stop changing,
   promote `data-src` → `src`, then snapshot. HTTP status comes from Chrome's
   performance log, so you get real `200`/`404`/`301` codes. Every rendered page
   is then checked against `webcrawler/challenge.py` — if what came back is a
   CAPTCHA or interstitial rather than content, it is never saved as a page.
   Pacing is per-host and adaptive: a `429` doubles the gap for that host, and a
   run of clean responses lets it drift back to full speed.
3. **Assets** — pulled with `requests` reusing the browser's cookies. Downloads are
   deduplicated across workers, and `url()` / `@import` inside CSS are followed
   recursively so webfonts and background images come along.
4. **Rewrite (second pass)** — this runs *after* the crawl, once every local path is
   known. That's why a link between two pages always ends up local, no matter which
   was fetched first. Anything not captured keeps its absolute URL and opens in a new
   tab, so nothing silently 404s. `integrity` attributes are stripped (they'd fail on
   local files), `<base>` is removed, and `srcset`, inline `<style>`, `style=""` and
   `meta refresh` are rewritten too.
5. **Site map** — `index.html` is generated from the manifest.

### Notes

- `--max-pages` is a budget for the whole mirror, not per run. To add more on a
  `--resume`, raise it above the number you already have.
- URL → path mapping: `/about` → `pages/host/about/index.html`, `/a/b.php?x=1` →
  `pages/host/a/b__q<hash>.html`. Collisions get a short hash suffix, and filenames
  are sanitised for macOS/Windows.
- `robots.txt` is respected by default.
- Login sessions: `--manual-login` forces one worker and a visible window.
- Anti-bot walls are detected, never solved. See
  [When a site blocks you](#when-a-site-blocks-you).
- Only crawl sites you own or are allowed to crawl, and keep `--delay` polite.
  A challenge screen is the site asking you not to automate it; clearing one by
  hand for your own crawl is fine, engineering around it is not.

## Tests

```bash
python tests/test_challenge.py      # or: python -m pytest tests/
```
