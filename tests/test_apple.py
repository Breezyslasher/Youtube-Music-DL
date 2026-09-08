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
