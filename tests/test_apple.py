"""Apple Music lyrics: TTML conversion, search/fetch, the provider chain,
and the settings round-trip. All HTTP is faked."""

import pytest
from fastapi.testclient import TestClient

import beetdrop.apple as apple
import beetdrop.lyrics as lyrics_module
from beetdrop.app import create_app
from beetdrop.config import Config

# The exact shape Apple returns, taken from a real /syllable-lyrics body.
WORD_TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml" '
    'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
    'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
    'itunes:timing="Word" xml:lang="en"><body><div>'
    '<p begin="20.783" end="22.688" itunes:key="L1" ttm:agent="v1">'
    '<span begin="20.783" end="21.094">Thought</span> '
    '<span begin="21.094" end="21.361">I</span> '
    '<span begin="21.361" end="21.628">found</span> '
    '<span begin="21.628" end="21.929">a</span> '
    '<span begin="21.929" end="22.688">way</span></p>'
    '</div></body></tt>')

LINE_TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml" '
    'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
    'itunes:timing="Line" xml:lang="en"><body><div>'
    '<p begin="63.100" end="70.000">This nearly was mine</p>'
    '</div></body></tt>')


class FakeResp:
    def __init__(self, data=None, ok=True, status=200, text=""):
        self._data = data
        self.ok = ok
        self.status_code = status
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class TestTtmlConversion:
    def test_word_ttml_to_line_level(self):
        assert apple.ttml_to_lrc(WORD_TTML) == "[00:20.78]Thought I found a way"

    def test_word_ttml_to_enhanced_lrc(self):
        lrc = apple.ttml_to_lrc(WORD_TTML, word_by_word=True)
        assert lrc == ("[00:20.78]<00:20.78>Thought <00:21.09>I "
                       "<00:21.36>found <00:21.63>a <00:21.93>way")

    def test_line_ttml_ignores_word_request(self):
        # Apple returns Line timing for tracks with no syllable data; asking
        # for words must not invent them.
        assert apple.ttml_to_lrc(LINE_TTML, word_by_word=True) == \
            "[01:03.10]This nearly was mine"

    def test_is_word_level(self):
        assert apple.is_word_level(WORD_TTML)
        assert not apple.is_word_level(LINE_TTML)

    def test_garbage_is_none(self):
        assert apple.ttml_to_lrc("not xml") is None
        assert apple.ttml_to_lrc("") is None
        assert apple.ttml_to_lrc("<tt></tt>") is None

    @pytest.mark.parametrize("value,expected", [
        ("20.783", 20.783), ("1:20.5", 80.5), ("1:02:03", 3723.0), ("", None),
    ])
    def test_clock_values(self, value, expected):
        assert apple._parse_time(value) == expected


class TestSearchAndFetch:
    def setup_method(self):
        apple._dev_token["value"] = "devtok"
        apple._dev_token["at"] = 9e18  # keep it fresh, never scrape

    def test_search_prefers_matching_duration(self, monkeypatch):
        payload = {"results": {"songs": {"data": [
            {"id": "wrong", "attributes": {"durationInMillis": 400000}},
            {"id": "right", "attributes": {"durationInMillis": 200500}},
        ]}}}
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: FakeResp(payload))
        assert apple.search_song("d", "m", "us", "A", "S", 200) == "right"

    def test_search_rejects_every_wrong_duration(self, monkeypatch):
        payload = {"results": {"songs": {"data": [
            {"id": "x", "attributes": {"durationInMillis": 400000}}]}}}
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: FakeResp(payload))
        assert apple.search_song("d", "m", "us", "A", "S", 200) is None

    def test_fetch_synced_end_to_end(self, monkeypatch):
        def fake_get(url, **kwargs):
            if "/search" in url:
                return FakeResp({"results": {"songs": {"data": [
                    {"id": "1369380479",
                     "attributes": {"durationInMillis": 200000}}]}}})
            if "syllable-lyrics" in url:
                return FakeResp({"data": [{"attributes": {"ttml": WORD_TTML}}]})
            return FakeResp(ok=False, status=404)
        monkeypatch.setattr(apple.requests, "get", fake_get)

        assert apple.fetch_synced("mut", "Billie Eilish", "lovely", 200) == \
            "[00:20.78]Thought I found a way"
        assert apple.fetch_synced("mut", "Billie Eilish", "lovely", 200,
                                  word_by_word=True).startswith(
            "[00:20.78]<00:20.78>Thought")

    def test_no_token_makes_no_request(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("should not be called")
        monkeypatch.setattr(apple.requests, "get", boom)
        assert apple.fetch_synced("", "A", "S") is None

    def test_missing_lyrics_is_none(self, monkeypatch):
        def fake_get(url, **kwargs):
            if "/search" in url:
                return FakeResp({"results": {"songs": {"data": [
                    {"id": "1", "attributes": {}}]}}})
            return FakeResp(ok=False, status=404)
        monkeypatch.setattr(apple.requests, "get", fake_get)
        assert apple.fetch_synced("mut", "A", "S") is None


class TestProviderChain:
    def _sources(self, monkeypatch, lrclib=None, mxm=None, apl=None):
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda a, t, al, d: lrclib)
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            lambda *a, **k: mxm)
        monkeypatch.setattr(lyrics_module.apple, "fetch_synced",
                            lambda *a, **k: apl)

    def test_apple_primary_wins(self, monkeypatch):
        self._sources(monkeypatch, lrclib="[00:01.00]L", apl="[00:02.00]A")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="apple", apple_token="t") == "[00:02.00]A"

    def test_apple_is_a_fallback_for_the_others(self, monkeypatch):
        self._sources(monkeypatch, lrclib=None, mxm=None, apl="[00:02.00]A")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="lrclib", apple_token="t") == "[00:02.00]A"

    def test_apple_skipped_without_token(self, monkeypatch):
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda a, t, al, d: None)
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            lyrics_module.apple, "fetch_synced",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
        assert lyrics_module.fetch_synced_lyrics("A", "S", provider="apple") is None

    def test_unknown_provider_falls_back_to_lrclib(self, monkeypatch):
        self._sources(monkeypatch, lrclib="[00:01.00]L")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="spotify") == "[00:01.00]L"

    def test_placeholder_from_apple_is_rejected(self, monkeypatch):
        # The synthetic-timing guard applies to every source.
        even = "\n".join("[00:%02d.00]word %d" % (i * 4, i) for i in range(12))
        self._sources(monkeypatch, apl=even, lrclib="[00:01.00]real")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="apple", apple_token="t") == "[00:01.00]real"


class TestSettings:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_token_stored_but_never_exposed(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["apple_token_set"] is False
            client.put("/api/settings", json={"apple_token": "secret-mut"})
            settings = client.get("/api/settings").json()
            assert settings["apple_token_set"] is True
            assert "secret-mut" not in str(settings)
            client.put("/api/settings", json={"apple_token": ""})
            assert client.get("/api/settings").json()["apple_token_set"] is False

    def test_apple_is_a_valid_provider(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            body = client.put("/api/settings",
                              json={"lyrics_provider": "apple"}).json()
            assert body["lyrics_provider"] == "apple"
            assert client.put("/api/settings",
                              json={"lyrics_provider": "tidal"}).status_code == 422

    def test_word_lyrics_and_storefront_persist(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["word_lyrics"] is False
            body = client.put("/api/settings",
                              json={"word_lyrics": True,
                                    "apple_storefront": "gb"}).json()
            assert body["word_lyrics"] is True
            assert body["apple_storefront"] == "gb"
        from beetdrop.db import Store
        stored = Store(config.db_path).get_settings()
        assert stored["word_lyrics"] == "1"
        assert stored["apple_storefront"] == "gb"


class TestTokenStaleness:
    """A media-user-token is long-lived but not permanent; an expired one
    must be reported, not silently swallowed."""

    def setup_method(self):
        apple._dev_token["value"] = ""
        apple._dev_token["at"] = 0.0

    def _dev(self, monkeypatch):
        monkeypatch.setattr(apple, "fetch_developer_token", lambda force=False: "dev")

    def test_no_token_says_so(self):
        assert apple.check_token("")["ok"] is False

    def test_expired_token_is_reported(self, monkeypatch):
        self._dev(monkeypatch)
        monkeypatch.setattr(apple, "_get_json", lambda *a, **k: (None, 401))
        result = apple.check_token("stale")
        assert result["ok"] is False
        assert "401" in result["detail"]

    def test_lyrics_refused_points_at_subscription(self, monkeypatch):
        self._dev(monkeypatch)
        calls = {"n": 0}

        def fake(url, headers, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"results": {"songs": {"data": [{"id": "1"}]}}}, 200
            return None, 403
        monkeypatch.setattr(apple, "_get_json", fake)
        result = apple.check_token("tok")
        assert result["ok"] is False
        assert "subscription" in result["detail"]

    def test_working_token(self, monkeypatch):
        self._dev(monkeypatch)
        calls = {"n": 0}

        def fake(url, headers, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"results": {"songs": {"data": [{"id": "1"}]}}}, 200
            return {"data": [{"attributes": {"ttml": WORD_TTML}}]}, 200
        monkeypatch.setattr(apple, "_get_json", fake)
        assert apple.check_token("tok")["ok"] is True

    def test_endpoint_surfaces_the_result(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        monkeypatch.setattr(app_module.apple, "check_token",
                            lambda token, storefront: {"ok": False,
                                                       "detail": "expired"})
        with TestClient(create_app(config)) as client:
            body = client.post("/api/lyrics/apple-test").json()
        assert body == {"ok": False, "detail": "expired"}


class TestWordByWordPreference:
    """Apple is the only per-word source, so asking for word timing must
    not be silently satisfied by a line-level hit from another source."""

    def _sources(self, monkeypatch, lrclib=None, mxm=None, apl=None, calls=None):
        def track(name, value):
            def inner(*a, **k):
                if calls is not None:
                    calls.append(name)
                return value
            return inner
        monkeypatch.setattr(lyrics_module, "_lrclib",
                            lambda a, t, al, d: track("lrclib", lrclib)())
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            track("musixmatch", mxm))
        monkeypatch.setattr(lyrics_module.apple, "fetch_synced",
                            track("apple", apl))

    def test_word_request_beats_a_line_level_hit_from_lrclib(self, monkeypatch):
        # The regression: LRCLIB answers first for mainstream tracks, so
        # Apple was never asked and word timing was silently lost.
        words = "[00:09.26]<00:09.26>I <00:09.64>drove"
        self._sources(monkeypatch, lrclib="[00:09.00]I drove", apl=words)
        assert lyrics_module.fetch_synced_lyrics(
            "Adele", "Remedy", provider="lrclib", apple_token="t",
            word_by_word=True) == words

    def test_falls_back_when_apple_has_only_line_timing(self, monkeypatch):
        self._sources(monkeypatch, lrclib="[00:09.00]lrclib line",
                      apl="[00:09.00]apple line")
        # Apple has no word timing for this one, so the configured primary
        # decides as usual.
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="lrclib", apple_token="t",
            word_by_word=True) == "[00:09.00]lrclib line"

    def test_apple_asked_only_once(self, monkeypatch):
        calls = []
        self._sources(monkeypatch, lrclib=None, apl="[00:09.00]apple line",
                      calls=calls)
        lyrics_module.fetch_synced_lyrics("A", "S", provider="lrclib",
                                          apple_token="t", word_by_word=True)
        assert calls.count("apple") == 1, calls

    def test_line_mode_keeps_the_configured_order(self, monkeypatch):
        self._sources(monkeypatch, lrclib="[00:09.00]lrclib", apl="[00:09.00]apple")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="lrclib", apple_token="t",
            word_by_word=False) == "[00:09.00]lrclib"

    def test_no_apple_token_is_unaffected(self, monkeypatch):
        self._sources(monkeypatch, lrclib="[00:09.00]lrclib")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="lrclib", word_by_word=True) == "[00:09.00]lrclib"


class TestHasWordTiming:
    @pytest.mark.parametrize("lrc,expected", [
        ("[00:09.26]<00:09.26>I <00:09.64>drove", True),
        ("[00:09.26]I drove by", False),
        ("", False),
    ])
    def test_detection(self, lrc, expected):
        assert lyrics_module.has_word_timing(lrc) is expected


class TestLyricsProbe:
    """The diagnostic that says whether a track is line-level because
    Apple lacks word timing, or because Apple was never reached."""

    def _run(self, capsys, config, **patches):
        from beetdrop.__main__ import cmd_lyrics_probe

        class Args:
            artist = "5 Seconds of Summer"
            title = "Social Casualty"
            duration = 189
        rc = cmd_lyrics_probe(Args(), config)
        return rc, capsys.readouterr().out

    def test_reports_apple_never_asked_without_a_token(self, tmp_path, capsys,
                                                       monkeypatch):
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda *a: None)
        monkeypatch.setattr(lyrics_module, "_musixmatch", lambda *a: None)
        rc, out = self._run(capsys, config)
        assert rc == 0
        assert "Apple is never asked" in out
        assert "nothing - no source had synced lyrics" in out

    def test_reports_word_level_when_apple_has_it(self, tmp_path, capsys,
                                                  monkeypatch):
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="tok",
                        word_lyrics=True)
        monkeypatch.setattr(apple, "fetch_developer_token", lambda force=False: "dev")
        monkeypatch.setattr(apple, "_get_json", lambda *a, **k: (
            {"results": {"songs": {"data": [
                {"id": "1", "attributes": {"name": "Social Casualty",
                                           "artistName": "5 Seconds of Summer",
                                           "durationInMillis": 189000}}]}}}, 200))
        monkeypatch.setattr(apple, "fetch_ttml", lambda *a, **k: WORD_TTML)
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda *a: None)
        monkeypatch.setattr(lyrics_module, "_musixmatch", lambda *a: None)
        rc, out = self._run(capsys, config)
        assert rc == 0
        assert "WORD (Apple has word-by-word)" in out
        assert "WORD-BY-WORD" in out

    def test_explains_a_duration_miss(self, tmp_path, capsys, monkeypatch):
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="tok")
        monkeypatch.setattr(apple, "fetch_developer_token", lambda force=False: "dev")
        # The only candidate is a minute longer: a different recording.
        monkeypatch.setattr(apple, "_get_json", lambda *a, **k: (
            {"results": {"songs": {"data": [
                {"id": "1", "attributes": {"name": "Social Casualty (Live)",
                                           "artistName": "5SOS",
                                           "durationInMillis": 249000}}]}}}, 200))
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda *a: None)
        monkeypatch.setattr(lyrics_module, "_musixmatch", lambda *a: None)
        rc, out = self._run(capsys, config)
        assert "duration tolerance" in out
        assert "+60.0s vs your file" in out


BG_TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml" '
    'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
    'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" itunes:timing="Word">'
    '<body><div><p begin="160.85" end="165.00">'
    '<span begin="160.85" end="161.25">It\'s</span> '
    '<span begin="161.25" end="161.80">so</span> '
    '<span begin="161.80" end="162.40">cold</span>'
    '<span ttm:role="x-bg">'
    '<span begin="162.50" end="163.00">(Out</span> '
    '<span begin="163.00" end="164.00">he-e-ere)</span>'
    '</span></p></div></body></tt>')


class TestBackgroundVocals:
    """Apple nests background vocals in their own span group. Emitting
    them inline made the clock run backwards mid-line, which a
    word-highlighting player renders as a jump."""

    def test_background_group_becomes_its_own_line(self):
        lrc = apple.ttml_to_lrc(BG_TTML, word_by_word=True)
        assert lrc.splitlines() == [
            "[02:40.85]<02:40.85>It's <02:41.25>so <02:41.80>cold",
            "[02:42.50]<02:42.50>(Out <02:43.00>he-e-ere)",
        ]

    def test_no_line_runs_backwards(self):
        lrc = apple.ttml_to_lrc(BG_TTML, word_by_word=True)
        assert not lyrics_module.has_backwards_word_timing(lrc)

    def test_line_level_still_keeps_the_background_text(self):
        assert "he-e-ere" in apple.ttml_to_lrc(BG_TTML)

    def test_a_word_without_a_begin_inherits_the_running_time(self):
        ttml = (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
            'itunes:timing="Word"><body><div><p begin="10.0">'
            '<span begin="10.0">one</span> <span>two</span> '
            '<span begin="9.0">three</span></p></div></body></tt>')
        lrc = apple.ttml_to_lrc(ttml, word_by_word=True)
        # "two" has no time and "three" claims an earlier one; neither may
        # rewind the line.
        assert lrc == "[00:10.00]<00:10.00>one <00:10.00>two <00:10.00>three"
        assert not lyrics_module.has_backwards_word_timing(lrc)


class TestBackwardsDetection:
    def test_flags_the_broken_shape(self):
        broken = ("[02:40.85]<02:40.85>It's <02:41.25>so <02:41.80>cold "
                  "<02:40.85>(Out he-e-ere)")
        assert lyrics_module.has_backwards_word_timing(broken)

    def test_ignores_sound_files(self):
        assert not lyrics_module.has_backwards_word_timing(
            "[02:40.85]<02:40.85>It <02:41.25>is <02:41.80>cold")
        assert not lyrics_module.has_backwards_word_timing("[00:01.00]plain")
        assert not lyrics_module.has_backwards_word_timing("")


class TestThrottleBackoff:
    """429 is a real outcome on a library scan and has to be waited out.
    Returning None instead would read as "this track has no lyrics"
    everywhere above, quietly marking a throttled run's tracks as
    misses."""

    def setup_method(self):
        apple._throttle_until = 0.0

    def teardown_method(self):
        apple._throttle_until = 0.0

    def _response(self, status, headers=None):
        class R:
            status_code = status
            ok = status == 200
            headers = {}

            def json(self):
                return {"ok": True}
        r = R()
        r.headers = headers or {}
        return r

    def test_retries_after_429_and_succeeds(self, monkeypatch):
        seen = []
        slept = []
        replies = [self._response(429, {"Retry-After": "2"}),
                   self._response(429),
                   self._response(200)]

        def fake_get(url, **kw):
            seen.append(url)
            return replies[len(seen) - 1]
        monkeypatch.setattr(apple.requests, "get", fake_get)
        monkeypatch.setattr(apple.time, "sleep", lambda s: slept.append(s))

        data, status = apple._get_json("https://x/y", {})
        assert (data, status) == ({"ok": True}, 200)
        assert len(seen) == 3                 # it retried rather than giving up
        # The wait is a shared deadline now, so the sleep is whatever is
        # left of it - just under Retry-After, never over.
        assert 1.5 < slept[0] <= 2            # honoured Retry-After
        assert slept[1] > 0

    def test_gives_up_after_the_retry_budget(self, monkeypatch):
        monkeypatch.setattr(apple.requests, "get",
                            lambda url, **kw: self._response(429))
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        data, status = apple._get_json("https://x/y", {})
        # Reported as 429, never as an ordinary empty result.
        assert data is None and status == 429

    def test_waits_are_capped(self, monkeypatch):
        slept = []
        monkeypatch.setattr(apple.requests, "get",
                            lambda url, **kw: self._response(
                                429, {"Retry-After": "99999"}))
        monkeypatch.setattr(apple.time, "sleep", lambda s: slept.append(s))
        apple._get_json("https://x/y", {})
        assert all(s <= apple.THROTTLE_MAX_WAIT for s in slept), slept

    def test_a_normal_error_is_not_retried(self, monkeypatch):
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            return self._response(404)
        monkeypatch.setattr(apple.requests, "get", fake_get)
        data, status = apple._get_json("https://x/y", {})
        assert (data, status) == (None, 404)
        assert len(seen) == 1     # 404 means no lyrics; retrying is pointless


class TestStaleDeveloperTokenRefresh:
    """The developer token is cached for six hours and Apple rotates it on
    its own schedule, so a long scan outlives it. A 401 has to cost a
    retry, not a track silently recorded as having no lyrics."""

    class Reply:
        def __init__(self, status, payload=None):
            self.status_code = status
            self.ok = status == 200
            self.headers = {}
            self._payload = payload or {}

        def json(self):
            return self._payload

    def _reset(self, monkeypatch, cached="old"):
        import time as _t
        apple._dev_token["value"] = cached
        apple._dev_token["at"] = _t.time()
        monkeypatch.setattr(apple, "_last_refresh", 0.0)

    def test_401_refreshes_the_token_and_retries(self, monkeypatch):
        self._reset(monkeypatch)
        sent = []

        def fake_get(url, headers=None, params=None, **kw):
            sent.append((headers or {}).get("Authorization"))
            if sent[-1] == "Bearer old":
                return self.Reply(401)
            return self.Reply(200, {"results": {"songs": {"data": [{"id": "42"}]}}})

        monkeypatch.setattr(apple.requests, "get", fake_get)
        monkeypatch.setattr(apple, "fetch_developer_token",
                            lambda force=False: "new")

        got = apple.search_song("old", "mut", "us", "Adele", "Hello")
        assert got == "42"                       # recovered, not a miss
        assert sent == ["Bearer old", "Bearer new"]

    def test_a_scan_rescrapes_once_not_once_per_track(self, monkeypatch):
        """The guard our Kodi addon does not need: it retries per request,
        which for thousands of tracks against a dead media-user-token would
        rescrape the web player thousands of times."""
        self._reset(monkeypatch)
        scrapes = []

        def fake_get(url, headers=None, params=None, **kw):
            return self.Reply(401)              # nothing ever recovers

        def fake_fetch(force=False):
            scrapes.append(force)
            apple._dev_token["value"] = "old"   # Apple hands back the same one
            return "old"

        monkeypatch.setattr(apple.requests, "get", fake_get)
        monkeypatch.setattr(apple, "fetch_developer_token", fake_fetch)

        for _ in range(200):                    # a scan's worth of tracks
            # Surfaced as unavailable, never as "this track has no lyrics".
            with pytest.raises(apple.AppleUnavailable):
                apple.search_song("old", "mut", "us", "A", "T")
        assert len(scrapes) == 1, scrapes

    def test_another_caller_refreshing_is_reused(self, monkeypatch):
        """Second call in a track: fetch_synced still holds the old token,
        but the cache already has the new one, so no rescrape."""
        self._reset(monkeypatch, cached="new")
        monkeypatch.setattr(apple, "fetch_developer_token",
                            lambda force=False: pytest.fail("rescraped"))
        assert apple._refresh_developer_token("old") == "new"

    def test_cooldown_blocks_a_second_rescrape(self, monkeypatch):
        import time as _t
        self._reset(monkeypatch)
        monkeypatch.setattr(apple, "_last_refresh", _t.time())
        monkeypatch.setattr(apple, "fetch_developer_token",
                            lambda force=False: pytest.fail("rescraped"))
        assert apple._refresh_developer_token("old") == "old"


class TestThrottleIsShared:
    """Per-request retries alone made a rate limit worse: with no pause
    between tracks, every one of thousands of lookups hit the limit and
    burned its full retry budget against it. Being told to slow down has
    to slow the whole run down, not just the call that was told."""

    class Limited:
        status_code = 429
        ok = False
        headers = {"Retry-After": "5"}

        def json(self):
            return {}

    def setup_method(self):
        apple._throttle_until = 0.0

    def teardown_method(self):
        apple._throttle_until = 0.0

    def test_a_429_holds_back_later_calls(self, monkeypatch):
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: self.Limited())
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        apple._get_json("https://x/y", {})
        # A later, unrelated call now has a deadline to wait for.
        assert apple._throttle_until > apple.time.time()

    def test_the_deadline_is_waited_out(self, monkeypatch):
        slept = []
        monkeypatch.setattr(apple.time, "sleep", lambda s: slept.append(s))
        apple.hold_off(4.0)
        apple._await_throttle()
        assert slept and 0 < slept[0] <= 4.0

    def test_hold_off_never_shortens_an_existing_deadline(self):
        apple.hold_off(30.0)
        first = apple._throttle_until
        apple.hold_off(1.0)
        assert apple._throttle_until == first

    def test_a_waiting_scan_is_capped(self, monkeypatch):
        slept = []
        monkeypatch.setattr(apple.time, "sleep", lambda s: slept.append(s))
        apple.hold_off(99999.0)
        apple._await_throttle()
        assert slept[0] <= apple.THROTTLE_MAX_WAIT


class TestErrorIsReportedInFull:
    """A status number alone cannot say whether a 429 is Apple limiting
    the account or a proxy or CDN answering on Apple's behalf. The body
    says which, so it has to reach the user rather than be swallowed."""

    class Limited:
        status_code = 429
        reason = "Too Many Requests"
        ok = False
        headers = {
            "Retry-After": "60",
            "CF-Ray": "8f2c1a9-LHR",
            "Content-Type": "application/json",
            # Must never be echoed back: it is shown in the UI and pasted
            # into bug reports.
            "Set-Cookie": "session=super-secret",
        }
        text = '{"errors":[{"title":"Rate limit exceeded"}]}'

    def setup_method(self):
        apple._throttle_until = 0.0
        apple.LAST_ERROR.clear()

    def teardown_method(self):
        apple._throttle_until = 0.0
        apple.LAST_ERROR.clear()

    def _trigger(self, monkeypatch):
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: self.Limited())
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        apple._get_json("https://amp-api.music.apple.com/v1/catalog/us/search",
                        {"Authorization": "Bearer devtok-secret"})
        return apple.describe_last_error()

    def test_body_and_useful_headers_are_kept(self, monkeypatch):
        report = self._trigger(monkeypatch)
        assert "HTTP 429" in report
        assert "Rate limit exceeded" in report      # Apple's own words
        assert "Retry-After: 60" in report
        assert "CF-Ray" in report                   # tells us who answered

    def test_credentials_are_never_echoed(self, monkeypatch):
        report = self._trigger(monkeypatch)
        assert "devtok-secret" not in report
        assert "super-secret" not in report
        assert "Set-Cookie" not in report

    def test_a_long_body_is_truncated(self, monkeypatch):
        class Huge(self.Limited):
            text = "x" * 50000
        monkeypatch.setattr(apple.requests, "get", lambda *a, **k: Huge())
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        apple._get_json("https://x/y", {})
        assert len(apple.LAST_ERROR["body"]) <= apple._BODY_SNIPPET

    def test_nothing_recorded_means_empty_report(self):
        assert apple.describe_last_error() == ""

    def test_the_token_check_shows_what_apple_sent(self, monkeypatch):
        monkeypatch.setattr(apple, "fetch_developer_token",
                            lambda force=False: "dev")
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: self.Limited())
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        result = apple.check_token("mut")
        assert result["ok"] is False
        assert "Rate limit exceeded" in result["detail"]
        assert "429" in result["detail"]


class TestWaitsMatchApplesRealResponse:
    """Modelled on an actual amp-api 429:

        Server: daiquiri/5, Via: 1.1 varnish, no Retry-After
        {"title":"Too Many Requests","detail":"Request is forbidden",
         "status":"429","code":"42900"}

    Apple states no wait at all, so what we guess is what governs."""

    class NoRetryAfter:
        status_code = 429
        ok = False
        headers = {"Server": "daiquiri/5", "Via": "1.1 varnish"}
        text = ('{"errors":[{"title":"Too Many Requests",'
                '"detail":"Request is forbidden","code":"42900"}]}')

    def setup_method(self):
        apple._throttle_until = 0.0

    def teardown_method(self):
        apple._throttle_until = 0.0

    def test_no_retry_after_still_means_a_real_pause(self):
        # It used to fall back to the first retry delay - about a second -
        # so the whole budget was ~15s against a limit lasting far longer.
        assert apple._retry_after(self.NoRetryAfter(),
                                  apple.THROTTLE_BACKOFF) >= apple.THROTTLE_BLIND_WAIT

    def test_a_stated_wait_is_honoured_beyond_one_sleep(self):
        class Stated(self.NoRetryAfter):
            headers = {"Retry-After": "120"}
        # Capping this at the single-sleep limit silently ignored most of
        # what Apple asked for.
        assert apple._retry_after(Stated(), apple.THROTTLE_BACKOFF) == 120

    def test_an_absurd_wait_is_capped(self):
        class Forever(self.NoRetryAfter):
            headers = {"Retry-After": "999999"}
        assert apple._retry_after(Forever(), 1.0) == apple.THROTTLE_MAX_HOLD

    def test_the_whole_deadline_is_waited_out_not_just_one_chunk(self, monkeypatch):
        """The bug: one sleep of at most MAX_WAIT, then the call went
        through anyway - a 60s hold paused 30s and walked back in."""
        slept = []
        now = [1000.0]
        monkeypatch.setattr(apple.time, "time", lambda: now[0])

        def fake_sleep(seconds):
            slept.append(seconds)
            now[0] += seconds        # a sleep really does move the clock
        monkeypatch.setattr(apple.time, "sleep", fake_sleep)

        apple.hold_off(90.0)
        apple._await_throttle()
        assert sum(slept) >= 90                       # the full hold
        assert max(slept) <= apple.THROTTLE_MAX_WAIT  # in interruptible chunks

    def test_a_sleep_that_does_not_advance_cannot_spin_forever(self, monkeypatch):
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        apple.hold_off(apple.THROTTLE_MAX_HOLD)
        apple._await_throttle()   # returns rather than hanging


class TestSkipAppleWhileRateLimited:
    """A rate-limit hold must not stall sources that could answer now.

    The fetch-missing pass asks LRCLIB, Musixmatch and Apple. Waiting out
    Apple's minute on every track made the whole scan run at the length
    of that hold even for tracks LRCLIB had all along. The upgrade pass
    still waits, because there Apple is the only possible source and
    skipping would report a false miss."""

    def setup_method(self):
        apple._throttle_until = 0.0

    def teardown_method(self):
        apple._throttle_until = 0.0

    def test_skipped_without_waiting_when_others_can_answer(self, monkeypatch):
        monkeypatch.setattr(apple.time, "sleep",
                            lambda s: pytest.fail("waited out the hold"))
        apple.hold_off(60.0)
        with pytest.raises(apple.AppleUnavailable):
            apple.fetch_synced("mut", "A", "S", wait=False)

    def test_the_upgrade_pass_still_waits(self, monkeypatch):
        slept = []
        now = [1000.0]
        monkeypatch.setattr(apple.time, "time", lambda: now[0])

        def fake_sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
        monkeypatch.setattr(apple.time, "sleep", fake_sleep)
        monkeypatch.setattr(apple, "fetch_developer_token",
                            lambda force=False: "dev")
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: FakeResp(ok=False, status=404))
        apple.hold_off(60.0)
        apple.fetch_synced("mut", "A", "S", wait=True)
        assert sum(slept) >= 60

    def test_no_hold_means_no_difference(self, monkeypatch):
        monkeypatch.setattr(apple, "fetch_developer_token",
                            lambda force=False: "dev")
        monkeypatch.setattr(apple.requests, "get",
                            lambda *a, **k: FakeResp(ok=False, status=404))
        assert apple.fetch_synced("mut", "A", "S", wait=False) is None


class TestChainSkipsAppleButKeepsGoing:
    def setup_method(self):
        apple._throttle_until = 0.0

    def teardown_method(self):
        apple._throttle_until = 0.0

    def test_lrclib_still_answers_while_apple_is_limited(self, monkeypatch):
        monkeypatch.setattr(lyrics_module, "_lrclib",
                            lambda *a, **k: "[00:01.00]found")
        monkeypatch.setattr(apple.time, "sleep",
                            lambda s: pytest.fail("waited out the hold"))
        apple.hold_off(60.0)
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", apple_token="t") == "[00:01.00]found"

    def test_nothing_else_answering_defers_rather_than_missing(self, monkeypatch):
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda *a, **k: None)
        monkeypatch.setattr(lyrics_module, "_musixmatch", lambda *a, **k: None)
        monkeypatch.setattr(apple.time, "sleep", lambda s: None)
        apple.hold_off(60.0)
        with pytest.raises(lyrics_module.LyricsUnavailable):
            lyrics_module.fetch_synced_lyrics("A", "S", apple_token="t")


class TestSearchNeedsNoAccount:
    """Proven against the live API: the same catalog search, same second
    and same address, came back 429 with the media-user-token and 200
    without it. Apple's limit is on the account, so searching anonymously
    spends none of it and leaves the whole allowance for the lyrics call
    that genuinely needs a subscription."""

    SONG = {"id": "1369380479",
            "attributes": {"durationInMillis": 200187, "name": "lovely",
                           "hasLyrics": True, "hasTimeSyncedLyrics": True}}

    def setup_method(self):
        apple._dev_token["value"] = "devtok"
        apple._dev_token["at"] = 9e18
        apple._throttle_until = 0.0

    def test_the_search_sends_no_media_user_token(self, monkeypatch):
        seen = {}

        def fake_get(url, headers=None, params=None, **kw):
            seen.update(headers or {})
            return FakeResp({"results": {"songs": {"data": [self.SONG]}}})
        monkeypatch.setattr(apple.requests, "get", fake_get)

        apple.search_song("dev", "mut-should-not-be-sent", "us", "A", "S", 200)
        assert seen.get("Authorization") == "Bearer dev"
        assert not seen.get("Media-User-Token")

    def test_the_lyrics_call_still_sends_it(self, monkeypatch):
        sent = []

        def fake_get(url, headers=None, params=None, **kw):
            sent.append(((headers or {}).get("Media-User-Token"), url))
            if "/search" in url:
                return FakeResp({"results": {"songs": {"data": [self.SONG]}}})
            return FakeResp({"data": [{"attributes": {"ttml": WORD_TTML}}]})
        monkeypatch.setattr(apple.requests, "get", fake_get)

        apple.fetch_synced("mut", "Billie Eilish", "lovely", 200)
        search = [t for t, url in sent if "/search" in url or True][0]
        assert search in ("", None)                      # search: anonymous
        assert any(token == "mut" for token, url in sent
                   if "syllable-lyrics" in url)          # lyrics: signed in


class TestSkipTracksAppleSaysHaveNoSyncedLyrics:
    def setup_method(self):
        apple._dev_token["value"] = "devtok"
        apple._dev_token["at"] = 9e18
        apple._throttle_until = 0.0

    def _run(self, monkeypatch, attributes):
        calls = []

        def fake_get(url, headers=None, params=None, **kw):
            calls.append(url)
            if "/search" in url:
                return FakeResp({"results": {"songs": {"data": [
                    {"id": "1", "attributes": attributes}]}}})
            return FakeResp({"data": [{"attributes": {"ttml": WORD_TTML}}]})
        monkeypatch.setattr(apple.requests, "get", fake_get)
        result = apple.fetch_synced("mut", "A", "S", 200)
        return result, calls

    def test_no_synced_lyrics_costs_one_request_not_two(self, monkeypatch):
        result, calls = self._run(monkeypatch, {
            "durationInMillis": 200000, "hasLyrics": True,
            "hasTimeSyncedLyrics": False})
        assert result is None
        assert not any("syllable-lyrics" in url for url in calls)

    def test_no_lyrics_at_all_is_also_skipped(self, monkeypatch):
        result, calls = self._run(monkeypatch, {
            "durationInMillis": 200000, "hasLyrics": False})
        assert result is None
        assert not any("syllable-lyrics" in url for url in calls)

    def test_synced_lyrics_are_still_fetched(self, monkeypatch):
        result, calls = self._run(monkeypatch, {
            "durationInMillis": 200000, "hasTimeSyncedLyrics": True})
        assert result and any("syllable-lyrics" in url for url in calls)

    def test_apple_saying_nothing_is_not_taken_as_no(self, monkeypatch):
        """Absence of the flag must not silently skip a track that has
        lyrics; only an explicit false may."""
        result, calls = self._run(monkeypatch, {"durationInMillis": 200000})
        assert result and any("syllable-lyrics" in url for url in calls)
