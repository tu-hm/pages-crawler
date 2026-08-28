"""Tests for anti-bot wall detection and the human hand-off.

Run with `python -m pytest tests/` or just `python tests/test_challenge.py`.

The detector matters most in the negative direction: a false positive stops a
crawl and bothers you about a page that was fine, so the "must not flag" cases
below are the ones worth keeping honest.
"""
from __future__ import annotations

import builtins
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webcrawler.challenge import (CAPTCHA, DENIED, INTERSTITIAL, RATE_LIMIT,
                                  Challenge, ChallengeGate, detect)

FILLER = "Lorem ipsum dolor sit amet consectetur. " * 200

CF_INTERSTITIAL = """<html><head><title>Just a moment...</title></head><body>
<div id="challenge-form"></div>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
<h1>Checking your browser before accessing example.com</h1></body></html>"""

CF_1020 = """<html><head><title>Attention Required! | Cloudflare</title></head><body>
<div class="cf-error-details">Sorry, you have been blocked. Error 1020
Ray ID: 7abc123</div></body></html>"""

CF_1015 = """<html><body>Error 1015 You are being rate limited.
Cloudflare Ray ID: 8ff</body></html>"""

TURNSTILE = """<html><body><h2>Verify you are human</h2>
<div class="cf-turnstile" data-sitekey="x"></div>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body></html>"""

HCAPTCHA = """<html><body>Please complete the security check.
<div class="h-captcha" data-sitekey="y"></div>
<script src="https://hcaptcha.com/1/api.js"></script></body></html>"""

# A real signup page that merely *contains* a captcha widget is content, not a wall.
SIGNUP = f"""<html><head><title>Create your account</title></head><body>
<h1>Sign up</h1><p>{FILLER}</p>
<form><input name=email><div class="g-recaptcha" data-sitekey="z"></div>
<script src="https://www.google.com/recaptcha/api.js"></script>
<button>Create account</button></form></body></html>"""

# An article *about* Cloudflare errors must not be mistaken for one.
ARTICLE = f"""<html><head><title>How Cloudflare rate limiting works</title></head>
<body><article><h1>Access denied errors and what they mean</h1>
<p>{FILLER} error 1020 {FILLER}</p>
<p>When you see "checking your browser", that is the interstitial.</p>
</article></body></html>"""

PLAIN = f"<html><head><title>Docs</title></head><body><h1>Hi</h1><p>{FILLER}</p></body></html>"

WALLS = [
    ("cloudflare interstitial", CF_INTERSTITIAL, None, INTERSTITIAL),
    ("cloudflare 1020 block", CF_1020, 403, DENIED),
    ("cloudflare 1015 rate limit", CF_1015, 429, RATE_LIMIT),
    ("turnstile captcha", TURNSTILE, 403, CAPTCHA),
    ("hcaptcha wall", HCAPTCHA, 403, CAPTCHA),
]

NOT_WALLS = [
    ("signup form with recaptcha", SIGNUP, 200),
    ("article about cloudflare", ARTICLE, 200),
    ("plain page", PLAIN, 200),
    ("empty document", "", 200),
]


def test_detects_walls():
    for name, html, status, expected in WALLS:
        found = detect(html, "", status)
        assert found is not None, f"{name}: nothing detected"
        assert found.kind == expected, f"{name}: got {found.kind}, want {expected}"


def test_ignores_real_pages():
    for name, html, status in NOT_WALLS:
        found = detect(html, "", status)
        assert found is None, f"{name}: false positive ({found})"


def test_interstitials_are_human_clearable_but_blocks_are_not():
    assert detect(CF_INTERSTITIAL, "", 503).human_clearable
    assert detect(TURNSTILE, "", 403).human_clearable
    assert not detect(CF_1020, "", 403).human_clearable   # same IP, same answer
    assert not detect(CF_1015, "", 429).human_clearable   # waiting is the fix


class _StubDriver:
    """Stands in for the solver window. Optionally 'clears' after a poll."""

    title = "stub"

    def __init__(self, clears_itself: bool):
        self.views = 0
        self.clears_itself = clears_itself

    def get(self, url):
        pass

    @property
    def page_source(self):
        self.views += 1
        if self.clears_itself and self.views > 1:
            return "<html><body>real page</body></html>"
        return '<div id="challenge-form"></div>/cdn-cgi/challenge-platform/'

    def get_cookies(self):
        return [{"name": "cf_clearance", "value": "abc"}]

    def quit(self):
        pass


WALL = Challenge(INTERSTITIAL, "cloudflare")


def test_self_clearing_check_needs_no_human():
    gate = ChallengeGate(lambda: _StubDriver(True), "http://x", auto_wait=3.0)
    assert gate.clear_wall("http://x/a", WALL, 0) is True
    assert gate.solved == 1 and gate.epoch == 1
    assert gate.cookies()[0]["name"] == "cf_clearance"


def test_enter_captures_the_session(monkeypatch=None):
    gate = ChallengeGate(lambda: _StubDriver(False), "http://x", auto_wait=0.1)
    original, builtins.input = builtins.input, lambda *a: ""
    try:
        assert gate.clear_wall("http://x/a", WALL, 0) is True
        assert gate.solved == 1
    finally:
        builtins.input = original


def test_s_abandons_walls_for_the_run():
    gate = ChallengeGate(lambda: _StubDriver(False), "http://x", auto_wait=0.1)
    original, builtins.input = builtins.input, lambda *a: "s"
    try:
        assert gate.clear_wall("http://x/a", WALL, 0) is False
        assert gate.abandoned
        assert gate.clear_wall("http://x/b", WALL, 0) is False   # no second prompt
    finally:
        builtins.input = original


def test_concurrent_workers_prompt_only_once():
    """Four workers hitting the same wall must not open four windows."""
    prompts = []
    gate = ChallengeGate(lambda: _StubDriver(False), "http://x", auto_wait=0.1)
    original = builtins.input
    builtins.input = lambda *a: (prompts.append(1), "")[1]
    try:
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(gate.clear_wall("http://x/a", WALL, 0)))
            for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(prompts) == 1, f"prompted {len(prompts)} times"
        assert all(results) and gate.solved == 1
    finally:
        builtins.input = original


def test_disabled_gate_never_opens_a_window():
    opened = []
    gate = ChallengeGate(lambda: opened.append(1) or _StubDriver(False),
                         "http://x", enabled=False)
    assert gate.clear_wall("http://x/a", WALL, 0) is False
    assert not opened


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok    {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
