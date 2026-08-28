"""Recognising anti-bot walls, and handing them to a human to clear.

This module *detects* interstitials, CAPTCHA walls and rate-limit blocks so the
crawler can react honestly instead of silently filing a "Just a moment..."
placeholder as if it were the real page.

Nothing here defeats a challenge.  When one shows up the crawler stops, opens a
real browser window, and waits for **you** to complete the check the way any
other visitor would.  The clearance cookie it then reuses is one you earned by
passing the check yourself -- which is also why it keeps working, where a
spoofing trick would not.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

log = logging.getLogger("crawler.challenge")

# --- kinds -----------------------------------------------------------------
INTERSTITIAL = "interstitial"   # "checking your browser", clears itself or with a click
CAPTCHA = "captcha"             # a puzzle a person has to complete
RATE_LIMIT = "rate-limit"       # slow down -- waiting is the fix
DENIED = "denied"               # hard block; a human at this IP cannot clear it
LOGIN_WALL = "login-wall"       # content is behind an account

# Kinds a person sitting at the browser can actually clear.
HUMAN_CLEARABLE = {INTERSTITIAL, CAPTCHA, LOGIN_WALL}


@dataclass
class Challenge:
    kind: str
    vendor: str
    detail: str = ""

    @property
    def human_clearable(self) -> bool:
        return self.kind in HUMAN_CLEARABLE

    def __str__(self) -> str:
        base = f"{self.vendor} {self.kind}"
        return f"{base} ({self.detail})" if self.detail else base

    @property
    def as_error(self) -> str:
        return f"challenge: {self}"


# --- markers ---------------------------------------------------------------
# Cloudflare's managed challenge / JS check.  Any one of these means the
# document *is* the interstitial, not the page we asked for.
_CF_INTERSTITIAL = (
    "/cdn-cgi/challenge-platform/",
    "cf-browser-verification",
    "cf_chl_opt",
    "id=\"challenge-form\"",
    "id='challenge-form'",
    "cf-challenge-running",
    "_cf_chl_opt",
)

# Cloudflare error pages carry a numbered code in the body.
_CF_ERROR_CODES = {
    "1005": DENIED, "1006": DENIED, "1007": DENIED, "1008": DENIED,
    "1010": DENIED,   # browser signature banned
    "1012": DENIED, "1015": RATE_LIMIT, "1020": DENIED,
}
_CF_ERROR_RE = re.compile(r"error\s*(?:code[: ]*)?1(0[01][0-9]|02[0-9])\b")

_CAPTCHA_VENDORS = (
    ("challenges.cloudflare.com", "cloudflare-turnstile"),
    ("cf-turnstile", "cloudflare-turnstile"),
    ("hcaptcha.com", "hcaptcha"),
    ("h-captcha", "hcaptcha"),
    ("google.com/recaptcha", "recaptcha"),
    ("gstatic.com/recaptcha", "recaptcha"),
    ("g-recaptcha", "recaptcha"),
    ("geo.captcha-delivery.com", "datadome"),
    ("captcha-delivery.com", "datadome"),
    ("px-captcha", "perimeterx"),
    ("_pxhd", "perimeterx"),
    ("funcaptcha", "arkose"),
    ("arkoselabs.com", "arkose"),
)

# Wording that means "this whole page is a wall", in the languages the wall
# vendors actually ship.
_WALL_PHRASES = (
    "verify you are human",
    "verifying you are human",
    "are you a robot",
    "checking your browser",
    "checking if the site connection is secure",
    "just a moment",
    "please enable javascript and cookies",
    "review the security of your connection",
    "additional security check is required",
    "unusual traffic from your computer network",
    "sorry, you have been blocked",
    "access denied",
    "attention required",
)

_RATE_PHRASES = (
    "too many requests",
    "rate limited",
    "rate limit exceeded",
    "you are being rate limited",
    "slow down",
)

_LOGIN_PHRASES = (
    "sign in to continue",
    "log in to continue",
    "you must be logged in",
    "please log in to view",
)

_TAGS = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.S | re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# A challenge page is always small.  Past this we are looking at real content
# that merely *mentions* a captcha (a signup form, a docs page about hCaptcha),
# and flagging it would be a false positive.
_WALL_TEXT_LIMIT = 2500


def visible_text(html: str, limit: int = 200_000) -> str:
    """Rough text content of a document, lowercased. Cheap, not exact."""
    stripped = _TAGS.sub(" ", html[:limit])
    return _WS.sub(" ", _ANY_TAG.sub(" ", stripped)).strip().lower()


def detect(html: str, title: str = "", status: Optional[int] = None,
           final_url: str = "") -> Optional[Challenge]:
    """Classify a rendered document as a challenge wall, or return None.

    Deliberately conservative: it would rather miss a wall than mislabel a real
    page, because a false positive stops the crawl and bothers you for nothing.
    """
    if not html:
        return None

    lower = html.lower()
    text = visible_text(html)
    short = len(text) <= _WALL_TEXT_LIMIT

    # 1. Cloudflare's own interstitial -- unambiguous, no length check needed.
    for marker in _CF_INTERSTITIAL:
        if marker in lower:
            kind = CAPTCHA if "turnstile" in lower or "cf-chl-widget" in lower else INTERSTITIAL
            return Challenge(kind, "cloudflare", "challenge platform")

    # 2. Cloudflare error pages (1015 rate limit, 1020 firewall rule, ...).
    #    Length-guarded: a real error page is tiny, whereas a long article that
    #    happens to *discuss* error 1020 must not be mistaken for one.
    if short and ("cloudflare" in lower or "cf-error-details" in lower or "ray id" in text):
        match = _CF_ERROR_RE.search(text)
        if match:
            code = match.group(0)[-4:]
            kind = _CF_ERROR_CODES.get(code, DENIED)
            return Challenge(kind, "cloudflare", f"error {code}")

    # 3. A CAPTCHA vendor's widget, on a page that is nothing but the widget.
    for marker, vendor in _CAPTCHA_VENDORS:
        if marker in lower and (short or status in (403, 429)):
            return Challenge(CAPTCHA, vendor, "captcha wall")

    title_l = (title or "").strip().lower()

    # 4. Status codes that mean "blocked", with wall wording to back them up.
    if status == 429 or any(p in text for p in _RATE_PHRASES):
        if status == 429 or short:
            return Challenge(RATE_LIMIT, "server", f"http {status}" if status else "too many requests")

    if status in (403, 503) and short:
        for phrase in _WALL_PHRASES:
            if phrase in text or phrase in title_l:
                kind = DENIED if status == 403 else INTERSTITIAL
                return Challenge(kind, _guess_vendor(lower), f"http {status}")

    # 5. No status (Firefox, or the perf log missed it) -- wording alone, but
    #    only on a page too small to be anything else.
    if short:
        if title_l in ("just a moment...", "just a moment", "attention required! | cloudflare"):
            return Challenge(INTERSTITIAL, "cloudflare", "interstitial title")
        for phrase in _WALL_PHRASES[:8]:   # the unambiguous half of the list
            if phrase in text:
                return Challenge(INTERSTITIAL, _guess_vendor(lower), "wall wording")
        for phrase in _LOGIN_PHRASES:
            if phrase in text:
                return Challenge(LOGIN_WALL, "site", "login required")

    return None


def _guess_vendor(lower_html: str) -> str:
    for marker, vendor in _CAPTCHA_VENDORS:
        if marker in lower_html:
            return vendor
    for name in ("cloudflare", "akamai", "datadome", "perimeterx", "imperva", "incapsula"):
        if name in lower_html:
            return name
    return "unknown"


# --- human hand-off --------------------------------------------------------
class ChallengeGate:
    """Serialises 'a person clears the wall' across the worker threads.

    Only one worker prompts at a time; the rest park until the session is
    cleared, then retry with the cookies the human's browser earned.
    """

    def __init__(self, driver_factory: Callable[[], object], start_url: str,
                 enabled: bool = True, auto_wait: float = 12.0):
        self._factory = driver_factory
        self._start_url = start_url
        self.enabled = enabled
        self.auto_wait = auto_wait
        self._lock = threading.Lock()
        self._clear = threading.Event()
        self._clear.set()
        self._cookies: List[dict] = []
        self.epoch = 0          # bumped every time new cookies land
        self.solved = 0
        self.abandoned = False  # you told it to give up on walls this run

    # -- worker side --------------------------------------------------------
    def wait_until_open(self, timeout: float = 300.0) -> None:
        """Park while another worker has a browser window open for you."""
        self._clear.wait(timeout=timeout)

    def cookies(self) -> List[dict]:
        with self._lock:
            return list(self._cookies)

    def seed(self, cookies: List[dict]) -> None:
        """Prime the gate with cookies from disk, without counting a solve."""
        with self._lock:
            self._cookies = list(cookies)
            self.epoch += 1

    def clear_wall(self, url: str, challenge: Challenge, seen_epoch: int) -> bool:
        """Get a person to clear `challenge`. True if fresh cookies are ready.

        `seen_epoch` is the caller's cookie generation: if someone else already
        cleared a wall since then, we return immediately so the caller just
        retries with the newer session.
        """
        if not self.enabled or self.abandoned:
            return False

        # Someone may have cleared it while we queued for the lock.
        self.wait_until_open()
        with self._lock:
            if self.epoch > seen_epoch:
                return True
            if self.abandoned:
                return False
            self._clear.clear()   # park the other workers

        try:
            return self._prompt(url, challenge)
        finally:
            self._clear.set()

    # -- the actual hand-off ------------------------------------------------
    def _prompt(self, url: str, challenge: Challenge) -> bool:
        driver = None
        try:
            driver = self._factory()
        except Exception as exc:
            log.error("could not open a window for you to clear the wall: %s", str(exc)[:200])
            return False

        try:
            driver.get(url)
        except Exception as exc:
            log.warning("solver window could not load %s (%s)", url, str(exc)[:120])

        print("\n" + "=" * 70)
        print(f"  {challenge.vendor.upper()} {challenge.kind.upper()} — the site wants a human")
        print(f"  {url}")
        print()
        print("  A browser window is open. Complete the check there exactly as")
        print("  you would when visiting normally, and wait for the real page.")
        print()
        print("  Then: ENTER to carry on   ·   's' to skip walled pages this run")
        print("=" * 70)

        # Many interstitials clear themselves within a few seconds. Give it a
        # moment before nagging, so a self-clearing check needs nothing from you.
        settled = self._wait_for_clear(driver, self.auto_wait)
        if settled:
            print("  ...it cleared on its own. Carrying on.")
        else:
            answer = input("  > ").strip().lower()
            if answer.startswith("s"):
                with self._lock:
                    self.abandoned = True
                print("  OK — walled pages will be recorded as blocked and skipped.")
                self._quit(driver)
                return False
            still = self._still_walled(driver)
            if still:
                print(f"  Heads up: that still looks like {still}. "
                      f"Saving the session anyway — re-run with --resume if pages fail.")

        try:
            cookies = driver.get_cookies()
        except Exception:
            cookies = []
        self._quit(driver)

        if not cookies:
            log.warning("no cookies came back from the solver window")
            return False

        with self._lock:
            self._cookies = cookies
            self.epoch += 1
            self.solved += 1
        log.info("session cleared — carrying %d cookies into the crawl", len(cookies))
        return True

    def _wait_for_clear(self, driver, seconds: float) -> bool:
        """Poll until the document stops being a challenge, or time runs out."""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            time.sleep(1.0)
            if self._still_walled(driver) is None:
                return True
        return False

    def _still_walled(self, driver) -> Optional[Challenge]:
        try:
            return detect(driver.page_source or "", driver.title or "")
        except Exception:
            return None

    @staticmethod
    def _quit(driver) -> None:
        try:
            driver.quit()
        except Exception:
            pass
