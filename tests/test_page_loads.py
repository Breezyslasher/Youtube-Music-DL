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
def engine():
    """One Chromium for the module.

    Exactly one, deliberately: entering sync_playwright() twice in a
    process fails with "Sync API inside the asyncio loop", so every
    fixture that wants a browser shares this and brings its own server.
    """
    found = browser_path()
    if not found:
        pytest.skip("no chromium binary to launch")
    with playwright_api.sync_playwright() as pw:
        try:
            started = pw.chromium.launch(executable_path=found)
        except Exception as exc:
            pytest.skip("chromium would not start: %s" % exc)
        yield started
        started.close()


@pytest.fixture(scope="module")
def browser(engine, tmp_path_factory):
    """The browser against an empty library; a fresh tab per test.

    Sharing a tab meant an overlay left open by one test intercepted the
    next test's clicks - the tests failed each other rather than the app.
    """
    tmp = tmp_path_factory.mktemp("ui")
    music = tmp / "music"
    music.mkdir()
    config = Config(music_root=music, scratch_root=tmp / "s",
                    config_dir=tmp / "c")
    with Served(config) as served:
        yield engine, served.url


def open_tab(engine, url, **options):
    tab = engine.new_page(**options)
    problems = []
    tab.on("pageerror", lambda err: problems.append(str(err)))
    tab.on("console",
           lambda msg: problems.append(msg.text) if msg.type == "error" else None)
    tab.goto(url, wait_until="networkidle")
    return tab, problems


@pytest.fixture
def page(browser):
    engine, url = browser
    tab, problems = open_tab(engine, url)
    yield tab, problems
    tab.close()


@pytest.fixture
def phone(browser):
    """A real phone viewport - a Pixel-ish 412x915.

    The redesign shipped once having only ever been looked at wide, and
    on a phone it rendered half the old layout and half the new one at
    the same time. Nothing in the markup said so; only the geometry did.
    """
    engine, url = browser
    tab, problems = open_tab(engine, url, viewport={"width": 412, "height": 915})
    yield tab, problems
    tab.close()


def tiny_png(size=64, rgb=(200, 90, 70)):
    """A real PNG, so a browser can decode it and report its size."""
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def stocked(engine, tmp_path_factory):
    """A browser against a library that actually has an album in it.

    The main fixture serves an empty library, which is what most of
    these want - and is exactly why nobody noticed that Library rows
    never rendered artwork. There were no rows.
    """
    tmp = tmp_path_factory.mktemp("stocked")
    album = tmp / "music" / "Dolly Parton" / "9 to 5 (1980)"
    album.mkdir(parents=True)
    (album / "01 - Track.opus").write_bytes(b"x" * 2048)
    (album / "cover.png").write_bytes(tiny_png())
    bare = tmp / "music" / "Nobody" / "No Cover (2004)"
    bare.mkdir(parents=True)
    (bare / "01 - Track.opus").write_bytes(b"x" * 2048)
    config = Config(music_root=tmp / "music", scratch_root=tmp / "s",
                    config_dir=tmp / "c")
    with Served(config) as served:
        yield engine, served.url


class TestLibraryArtwork:
    """Covers render, which is not the same as the bytes being available.

    The endpoint can be perfect and the rows still blank: for a long
    time nothing served cover.jpg at all, so the markup had nothing to
    point at and always drew the placeholder. Asserting a decoded image
    is the only version of this test that would have failed then.
    """

    def rows(self, stocked):
        engine, url = stocked
        tab, problems = open_tab(engine, url)
        tab.click(".sidebar .navitem:has-text('Library')")
        tab.locator(".librow").first.wait_for(timeout=8000)
        return tab, problems

    def test_a_cover_on_disk_is_decoded_by_the_browser(self, stocked):
        tab, problems = self.rows(stocked)
        try:
            handle = tab.locator("img.art").first
            handle.wait_for(timeout=8000)
            size = tab.eval_on_selector(
                "img.art",
                "el => el.complete && [el.naturalWidth, el.naturalHeight]")
            assert size and size[0] > 0, "the cover did not load: %s" % (size,)
            assert not problems, problems[:3]
        finally:
            tab.close()

    def test_an_album_without_one_still_shows_the_placeholder(self, stocked):
        # Never a broken-image icon, and never an empty box either.
        tab, _ = self.rows(stocked)
        try:
            assert tab.locator("img.art").count() == 1
            assert tab.locator("span.art").count() == 1
        finally:
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

    def test_the_queue_is_a_destination_not_a_sheet(self, page):
        tab, _ = page
        tab.click(".sidebar .navitem:has-text('Queue')")
        tab.locator(".queuepanel").first.wait_for(state="visible", timeout=5000)
        # Nothing floats: the old fixed bar and sheet are gone entirely.
        assert tab.locator(".queuebar").count() == 0


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
        panel = tab.locator(".settingspage")
        panel.wait_for(state="visible", timeout=5000)
        # Settings is a page now, so the fields have to be found there and
        # not in an overlay - and every one of them has to have survived
        # the move. Disclosures are read too: the copy is folded away, not
        # deleted, and `inner_text` on a closed <details> would miss it.
        text = tab.evaluate(
            "() => document.querySelector('.settingspage').textContent")
        for label in ("Output format", "Primary lyrics source", "Layout",
                      "Apple Music token", "Max video quality",
                      "Music library path", "Concurrent downloads",
                      "Cookies", "Password", "Update yt-dlp"):
            assert label in text, "%s went missing in the redesign" % label

    def test_settings_no_longer_floats_over_the_page(self, page):
        # It used to be one very long sheet over whatever you were doing.
        # The only overlay left is the password prompt, which is not shown.
        tab, _ = page
        self.nav(tab, "Settings")
        assert tab.locator(".overlay").count() == 0

    def test_the_queue_rail_is_present_on_search(self, page):
        tab, _ = page
        assert tab.locator(".queuerail").is_visible()

    def test_the_rail_gives_way_where_the_page_wants_the_width(self, page):
        tab, _ = page
        for label in ("Stats", "Settings", "Queue"):
            self.nav(tab, label)
            assert not tab.locator(".queuerail").is_visible(), label

    def test_no_template_expression_leaks_on_any_screen(self, page):
        tab, _ = page
        for label in ("Library", "Stats", "Repair", "Queue", "Search"):
            self.nav(tab, label)
            assert "{{" not in tab.locator(".workarea").inner_text(), label


class TestThePhoneShell:
    """What the phone actually shows, in pixels.

    Every assertion here is a defect a person reported from a real
    handset: the tab bar rendering at the top of the page, the search
    field pinned near the bottom, and the old brand bar and queue footer
    still drawn underneath the new shell.
    """

    def box(self, tab, selector):
        found = tab.locator(selector).first.bounding_box()
        assert found, "%s is not laid out" % selector
        return found

    def test_the_desktop_chrome_is_not_rendered(self, phone):
        tab, _ = phone
        assert not tab.locator(".sidebar").is_visible()
        assert not tab.locator(".queuerail").is_visible()

    def test_the_old_layout_is_gone_from_the_document(self, phone):
        # Not merely hidden: removed. Two layouts in one document is what
        # produced a page that was neither.
        tab, _ = phone
        for stale in (".topbar", ".queuebar", ".searchbar"):
            assert tab.locator(stale).count() == 0, "%s survived" % stale

    def test_the_tab_bar_is_pinned_to_the_bottom(self, phone):
        tab, _ = phone
        bar = self.box(tab, ".tabbar")
        height = tab.evaluate("() => window.innerHeight")
        assert bar["y"] > height * 0.8, "the tab bar is not at the bottom"
        assert bar["y"] + bar["height"] <= height + 1

    def test_the_search_field_is_at_the_top_under_the_title(self, phone):
        tab, _ = phone
        title = self.box(tab, ".workhead .pagetitle")
        field = self.box(tab, ".workhead .searchrow input")
        assert field["y"] > title["y"], "the search field is above the title"
        assert field["y"] < 200, "the search field is not in the header"

    def test_the_page_does_not_scroll_sideways(self, phone):
        tab, _ = phone
        overflow = tab.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, "the page is %dpx too wide" % overflow

    def test_nothing_is_hidden_under_the_dock(self, phone):
        # The last row of a list used to sit underneath the fixed bar,
        # unreachable, because the padding that cleared it was a guess.
        tab, _ = phone
        tab.click(".tabbar button:has-text('Library')")
        tab.locator(".hint:has-text('Nothing here yet')").wait_for(timeout=5000)
        dock = self.box(tab, ".phonedock")
        measured = tab.evaluate(
            "() => getComputedStyle(document.querySelector('.workbody'))"
            ".paddingBottom")
        assert float(measured.rstrip("px")) >= dock["height"]

    def test_every_tab_reaches_its_screen(self, phone):
        tab, _ = phone
        for label in ("Queue", "Library", "Stats", "Settings", "Search"):
            tab.click(".tabbar button:has-text('%s')" % label)
            heading = tab.locator(".workhead .pagetitle").inner_text()
            assert heading.strip() == label

    def test_every_tab_draws_a_real_icon(self, phone):
        # Material Design Icons are inlined as path data, so a typo is a
        # tab with an empty box on it rather than an error anyone sees.
        tab, _ = phone
        drawn = tab.eval_on_selector_all(
            ".tabbar button svg path",
            "els => els.map(el => el.getAttribute('d') || '')")
        assert len(drawn) == 5, drawn
        assert all(len(d) > 20 for d in drawn), drawn
        # MDI is a filled set; stroking it would render the shapes twice.
        fills = tab.eval_on_selector_all(
            ".tabbar button svg",
            "els => els.map(el => getComputedStyle(el).fill)")
        assert all(fill not in ("none", "") for fill in fills), fills

    def test_tap_targets_are_big_enough(self, phone):
        tab, _ = phone
        heights = tab.eval_on_selector_all(
            ".tabbar button", "els => els.map(el => el.getBoundingClientRect().height)")
        assert heights and min(heights) >= 44, heights


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
