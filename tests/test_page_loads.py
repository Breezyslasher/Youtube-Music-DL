"""The page actually renders in a browser.

Everything else about the UI is checked by reading the source for a
string, which is enough to catch a button wired to nothing and useless
against the failure that has actually happened twice: the page not
running at all. A dropped ternary branch left app.js unparseable, and
nothing noticed until a person reported that a button did the wrong
thing - the browser had quietly gone on running the last copy it had.

So this loads the real page in Chromium, against the real app, and
asserts Vue mounted and the shell rendered. It is deliberately shallow:
one test that fails loudly when the page is broken beats a suite that
tests markup no browser ever parsed.
"""

import pathlib
import socket
import threading
import time

import pytest
import uvicorn

from beetdrop.app import create_app
from beetdrop.config import Config

playwright_api = pytest.importorskip("playwright.sync_api",
                                     reason="playwright unavailable")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Served:
    """The app on a real port, since a browser cannot talk to TestClient."""

    def __init__(self, config):
        self.port = free_port()
        self._server = uvicorn.Server(uvicorn.Config(
            create_app(config), host="127.0.0.1", port=self.port,
            log_level="error"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("the server never started")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.port


# The image ships a pinned Chromium whose build number need not match the
# one this playwright expects, and it must not try to download another.
CHROMIUM = "/opt/pw-browsers/chromium"


def browser_path():
    import os
    import shutil

    for candidate in (CHROMIUM, os.environ.get("CHROMIUM_PATH", ""),
                      shutil.which("chromium"), shutil.which("google-chrome")):
        if candidate and pathlib.Path(candidate).exists():
            return candidate
    return ""


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    """One browser and one server for the module; a fresh tab per test.

    Sharing a tab meant an overlay left open by one test intercepted the
    next test's clicks - the tests failed each other rather than the app.
    """
    tmp = tmp_path_factory.mktemp("ui")
    music = tmp / "music"
    music.mkdir()
    config = Config(music_root=music, scratch_root=tmp / "s",
                    config_dir=tmp / "c")
    found = browser_path()
    if not found:
        pytest.skip("no chromium binary to launch")
    with Served(config) as served:
        with playwright_api.sync_playwright() as pw:
            try:
                engine = pw.chromium.launch(executable_path=found)
            except Exception as exc:
                pytest.skip("chromium would not start: %s" % exc)
            yield engine, served.url
            engine.close()


@pytest.fixture
def page(browser):
    engine, url = browser
    tab = engine.new_page()
    problems = []
    tab.on("pageerror", lambda err: problems.append(str(err)))
    tab.on("console",
           lambda msg: problems.append(msg.text) if msg.type == "error" else None)
    tab.goto(url, wait_until="networkidle")
    yield tab, problems
    tab.close()


class TestThePageRuns:
    def test_nothing_threw_while_loading(self, page):
        tab, problems = page
        assert not problems, "the page logged errors: %s" % problems[:3]

    def test_vue_mounted(self, page):
        # v-cloak is removed only once Vue has taken over the markup, so
        # its absence is the mount actually happening rather than the
        # HTML merely being served.
        tab, _ = page
        assert tab.locator(".shell").count() == 1
        assert tab.locator(".shell[v-cloak]").count() == 0

    def test_the_search_modes_are_all_there(self, page):
        tab, _ = page
        labels = tab.locator(".modetoggle button").all_inner_texts()
        assert [text.strip() for text in labels] == [
            "Songs", "Albums", "Videos", "Lyrics"]

    def test_a_template_expression_did_not_leak_as_text(self, page):
        # A mis-typed binding renders as literal {{ ... }} rather than
        # failing, which reads as working software until someone looks.
        tab, _ = page
        assert "{{" not in tab.locator("body").inner_text()

    def test_settings_opens_and_renders_its_fields(self, page):
        tab, _ = page
        tab.click("button[aria-label='Settings']")
        panel = tab.locator(".overlay").first
        panel.wait_for(state="visible", timeout=5000)
        text = panel.inner_text()
        # A handful of the fields that must survive the settings split.
        for label in ("Output format", "Primary lyrics source", "Layout"):
            assert label in text, "%s is missing from Settings" % label

    def test_the_queue_can_be_opened(self, page):
        tab, _ = page
        tab.click(".queuebar")
        tab.locator(".queuepanel").first.wait_for(state="visible", timeout=5000)


class TestTheWorkbenchShell:
    """The redesign's shell: one `view` ref is the whole router, so a
    screen that fails to render is a screen nobody can reach."""

    def nav(self, tab, label):
        tab.click(".sidebar .navitem:has-text('%s')" % label)

    def test_every_nav_destination_exists(self, page):
        tab, _ = page
        labels = [text.strip() for text in
                  tab.locator(".sidebar .navitem .navtext").all_inner_texts()]
        assert labels == ["Search", "Queue", "Library", "Stats",
                          "Repair", "Settings"]

    def test_library_opens_and_reads_the_library(self, page):
        tab, problems = page
        self.nav(tab, "Library")
        tab.locator("h2:has-text('Library')").first.wait_for(state="visible",
                                                             timeout=5000)
        # An empty library must say what to do rather than render a
        # padded panel of nothing.
        tab.locator(".hint:has-text('Nothing here yet')").wait_for(timeout=5000)
        assert not problems, problems[:3]

    def test_stats_opens_and_renders_its_numbers(self, page):
        tab, problems = page
        self.nav(tab, "Stats")
        tab.locator(".statstrip").wait_for(state="visible", timeout=8000)
        # The labels are uppercased in CSS, so compare case-insensitively
        # rather than asserting the styling.
        text = tab.locator(".statstrip").inner_text().lower()
        for label in ("tracks", "albums", "artists", "on disk", "verified"):
            assert label in text
        assert not problems, problems[:3]

    def test_repair_is_reachable_from_the_sidebar(self, page):
        tab, _ = page
        self.nav(tab, "Repair")
        tab.locator("h3:has-text('Wrong match')").first.wait_for(timeout=5000)

    def test_settings_keeps_every_field(self, page):
        tab, _ = page
        self.nav(tab, "Settings")
        text = tab.locator(".overlay").first.inner_text()
        for label in ("Output format", "Primary lyrics source", "Layout",
                      "Apple Music token", "Max video quality"):
            assert label in text, "%s went missing in the redesign" % label

    def test_the_queue_rail_is_present_on_search(self, page):
        tab, _ = page
        assert tab.locator(".queuerail").is_visible()

    def test_no_template_expression_leaks_on_any_screen(self, page):
        tab, _ = page
        for label in ("Library", "Stats", "Repair", "Queue", "Search"):
            self.nav(tab, label)
            assert "{{" not in tab.locator(".workarea").inner_text(), label


class TestTheJudgementLine:
    """The third line on a result, and the reason the redesign exists:
    say what is questionable before the grab, not after it has been
    filed to _review."""

    def verdict(self, tab, result):
        return tab.evaluate("r => window.beetdrop.verdictFor(r)", result)

    def test_a_live_take_is_called_out(self, page):
        tab, _ = page
        found = self.verdict(tab, {"raw_title": "Hello (Live at Wembley)",
                                   "duration_seconds": 240})
        assert found and "Live or remix" in found["text"]

    def test_an_hour_long_mix_is_flagged(self, page):
        tab, _ = page
        found = self.verdict(tab, {"title": "Deep house set",
                                   "duration_seconds": 3600})
        assert found and found["warn"] is True

    def test_an_ordinary_track_says_nothing(self, page):
        # Padding every row with filler would make the line worth
        # nothing on the rows that matter.
        tab, _ = page
        assert self.verdict(tab, {"title": "9 to 5",
                                  "duration_seconds": 161}) is None
