"""Synced-lyrics fetch, sidecar writing, and the setting round-trip."""

import pytest
from fastapi.testclient import TestClient

import beetdrop.lyrics as lyrics_module
from beetdrop.lyrics import strip_source
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.library import write_lyrics_sidecar


class FakeResp:
    # A real Response always carries a status_code, and the lyrics chain
    # now reads it to tell "no lyrics" (404) from "ask again later" (5xx,
    # 429), so the stub has to carry one too.
    def __init__(self, ok=True, data=None, status=None):
        self.ok = ok
        self.status_code = status if status is not None else (200 if ok else 404)
        self._data = data or {}

    def json(self):
        return self._data


class TestFetchSynced:
    def test_returns_synced_lrc(self, monkeypatch):
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: FakeResp(data={
                                "syncedLyrics": "[00:01.00]Hello\n[00:03.00]World",
                                "plainLyrics": "Hello\nWorld"}))
        lrc = strip_source(
            lyrics_module.fetch_synced_lyrics("Artist", "Song", "Album", 200))
        assert lrc.startswith("[00:01.00]Hello")

    def test_plain_only_is_skipped(self, monkeypatch):
        # Timed only: a result with no syncedLyrics returns None.
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: FakeResp(data={
                                "syncedLyrics": "", "plainLyrics": "Hello"}))
        assert lyrics_module.fetch_synced_lyrics("A", "S", "Al", 200) is None

    def test_404_is_none(self, monkeypatch):
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: FakeResp(ok=False))
        assert lyrics_module.fetch_synced_lyrics("A", "S") is None


class TestLrclibCascade:
    """The strict /api/get misses a lot; each fallback loosens one
    constraint and /api/search is the fuzzy last resort."""

    def _router(self, monkeypatch, handler):
        calls = []

        def fake_get(url, params=None, **kwargs):
            calls.append((url, dict(params or {})))
            return FakeResp(**handler(url, dict(params or {})))
        monkeypatch.setattr(lyrics_module.requests, "get", fake_get)
        return calls

    def test_falls_back_to_lookup_without_album(self, monkeypatch):
        # The album tag disagrees with LRCLIB; dropping it must still hit.
        def handler(url, params):
            if params.get("album_name"):
                return {"ok": False}
            return {"data": {"syncedLyrics": "[00:01.00]A\n[00:05.00]B"}}
        calls = self._router(monkeypatch, handler)
        assert strip_source(lyrics_module.fetch_synced_lyrics(
            "Artist", "Song", "Wrong Album", 200)) == "[00:01.00]A\n[00:05.00]B"
        assert len(calls) == 2  # strict, then album dropped

    def test_falls_back_to_primary_artist_and_bare_title(self, monkeypatch):
        def handler(url, params):
            if (params.get("artist_name") == "Billie Eilish"
                    and params.get("track_name") == "lovely"):
                return {"data": {"syncedLyrics": "[00:01.00]Thought"}}
            return {"ok": False}
        self._router(monkeypatch, handler)
        assert strip_source(lyrics_module.fetch_synced_lyrics(
            "Billie Eilish, Khalid", "lovely (with Khalid)", "", 200)) \
            == "[00:01.00]Thought"

    def test_search_used_when_every_get_misses(self, monkeypatch):
        # Duration is 6s off, so /api/get (±2s) can never match; the fuzzy
        # search still finds it and picks the closest candidate.
        def handler(url, params):
            if url == lyrics_module.LRCLIB_SEARCH:
                return {"data": [
                    {"trackName": "Song", "artistName": "Artist",
                     "syncedLyrics": "[00:01.00]far", "duration": 260.0},
                    {"trackName": "Song", "artistName": "Artist",
                     "syncedLyrics": "[00:01.00]close", "duration": 206.0},
                ]}
            return {"ok": False}
        self._router(monkeypatch, handler)
        assert strip_source(lyrics_module.fetch_synced_lyrics(
            "Artist", "Song", "", 200)) == "[00:01.00]close"

    def test_search_rejects_a_different_recording(self, monkeypatch):
        # Only a wildly different duration is on offer: not our track.
        def handler(url, params):
            if url == lyrics_module.LRCLIB_SEARCH:
                return {"data": [{"trackName": "Song", "artistName": "Artist",
                                  "syncedLyrics": "[00:01.00]x", "duration": 400.0}]}
            return {"ok": False}
        self._router(monkeypatch, handler)
        assert lyrics_module.fetch_synced_lyrics("Artist", "Song", "", 200) is None

    def test_search_rejects_a_different_song(self, monkeypatch):
        # /api/search matches loosely and will return unrelated tracks.
        # Attaching their lyrics would be worse than finding nothing -
        # this is the bug that wrote real lyrics onto "Track 2"/"Artist".
        def handler(url, params):
            if url == lyrics_module.LRCLIB_SEARCH:
                return {"data": [
                    {"trackName": "Something Else Entirely",
                     "artistName": "A Different Band",
                     "syncedLyrics": "[00:01.00]wrong", "duration": 200.0}]}
            return {"ok": False}
        self._router(monkeypatch, handler)
        assert lyrics_module.fetch_synced_lyrics("Artist", "Track 2", "", 200) is None

    def test_search_allows_minor_title_and_artist_variation(self, monkeypatch):
        def handler(url, params):
            if url == lyrics_module.LRCLIB_SEARCH:
                return {"data": [
                    {"trackName": "lovely (with Khalid)",
                     "artistName": "Billie Eilish",
                     "syncedLyrics": "[00:01.00]Thought", "duration": 200.0}]}
            return {"ok": False}
        self._router(monkeypatch, handler)
        assert strip_source(lyrics_module.fetch_synced_lyrics(
            "Billie Eilish", "lovely", "", 200)) == "[00:01.00]Thought"

    def test_search_candidates_without_synced_are_ignored(self, monkeypatch):
        def handler(url, params):
            if url == lyrics_module.LRCLIB_SEARCH:
                return {"data": [{"trackName": "Song", "artistName": "Artist",
                                  "plainLyrics": "words", "duration": 200.0},
                                 {"trackName": "Song", "artistName": "Artist",
                                  "syncedLyrics": "", "duration": 200.0}]}
            return {"ok": False}
        self._router(monkeypatch, handler)
        assert lyrics_module.fetch_synced_lyrics("Artist", "Song", "", 200) is None


class TestNameNormalising:
    @pytest.mark.parametrize("raw,expected", [
        ("Billie Eilish, Khalid", "Billie Eilish"),
        ("Tegan and Sara feat. The Lonely Island", "Tegan and Sara"),
        ("Hall & Oates", "Hall & Oates"),   # "&" never split
        ("AC/DC", "AC/DC"),                 # "/" never split
    ])
    def test_primary_artist(self, raw, expected):
        assert lyrics_module._primary_artist(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("lovely (with Khalid)", "lovely"),
        ("Song (Remastered 2011)", "Song"),
        ("Song feat. Someone", "Song"),
        ("Plain Title", "Plain Title"),
    ])
    def test_simplify_title(self, raw, expected):
        assert lyrics_module._simplify_title(raw) == expected

    def test_network_error_is_not_reported_as_no_lyrics(self, monkeypatch):
        """A dead connection used to return None, which every caller read
        as "this track has no lyrics" - so a scan run with the network
        down recorded the whole library as missing lyrics and called it a
        successful run. It has to be distinguishable."""
        def boom(*a, **k):
            raise lyrics_module.requests.RequestException("down")
        monkeypatch.setattr(lyrics_module.requests, "get", boom)
        with pytest.raises(lyrics_module.LyricsUnavailable):
            lyrics_module.fetch_synced_lyrics("A", "S")

    def test_missing_fields_no_request(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or FakeResp())
        assert lyrics_module.fetch_synced_lyrics("", "Song") is None
        assert lyrics_module.fetch_synced_lyrics("Artist", "") is None
        assert called["n"] == 0  # never hit the network without artist+title


class TestSidecar:
    def test_writes_lrc_next_to_audio(self, tmp_path):
        audio = tmp_path / "01 - Song.opus"
        audio.write_bytes(b"x")
        target = write_lyrics_sidecar(audio, "[00:01.00]Hi")
        assert target == tmp_path / "01 - Song.lrc"
        assert target.read_text() == "[00:01.00]Hi"
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_does_not_overwrite(self, tmp_path):
        audio = tmp_path / "s.opus"
        audio.write_bytes(b"x")
        (tmp_path / "s.lrc").write_text("original")
        write_lyrics_sidecar(audio, "new")
        assert (tmp_path / "s.lrc").read_text() == "original"


class TestLyricsSetting:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_default_on_and_toggle_persists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MUSIC_PATH", raising=False)
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["lyrics"] is True
            body = client.put("/api/settings", json={"lyrics": False}).json()
            assert body["lyrics"] is False
        # Persisted across a restart.
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["lyrics"] is False

    def test_env_disables(self, monkeypatch):
        monkeypatch.setenv("BEETDROP_LYRICS", "0")
        assert Config().lyrics_enabled is False
        monkeypatch.setenv("BEETDROP_LYRICS", "1")
        assert Config().lyrics_enabled is True


class TestCrowdedWordTiming:
    """Word tags packed too tight for anyone to sing.

    What a forced aligner leaves on a line with more words than its
    window can hold: the tail gets pinned at whatever minimum step the
    writer enforces. It passes every other check - times increase, line
    timing is real, words are right - so nothing found these, and a
    highlighter given two words 10 ms apart shows one and skips the other.

    The thresholds were set against real files: 5,000-odd word gaps from
    a forced aligner and from Apple, where Apple's own good files score
    zero and the aligner's crammed parenthetical lines score every time.
    """

    def line(self, *times):
        tags = "".join("<00:%05.2f>w%d " % (t, i) for i, t in enumerate(times))
        return "[00:%05.2f]%s" % (times[0], tags)

    def test_a_collapsed_pair_is_caught(self):
        # 10 ms apart: the same instant, whatever the file says.
        assert lyrics_module.has_crowded_word_timing(
            self.line(10.0, 10.01, 10.5)) is True

    def test_several_tight_gaps_in_one_line_are_caught(self):
        # 40 ms each. One could be a contraction; three is not singing.
        assert lyrics_module.has_crowded_word_timing(
            self.line(10.0, 10.04, 10.08, 10.12)) is True

    def test_one_tight_gap_is_allowed(self):
        # A fast contraction is real, and flagging it would make the
        # count worth nothing on the files that matter.
        assert lyrics_module.has_crowded_word_timing(
            self.line(10.0, 10.04, 10.6, 11.2)) is False

    def test_ordinary_singing_is_not_caught(self):
        assert lyrics_module.has_crowded_word_timing(
            self.line(10.0, 10.33, 10.61, 11.02)) is False

    def test_backwards_timing_is_left_to_its_own_check(self):
        # Counting it here too would make one bad file look like two
        # problems, and the backwards check already reports it.
        backwards = self.line(10.0, 9.0, 10.5)
        assert lyrics_module.has_backwards_word_timing(backwards) is True
        assert lyrics_module.has_crowded_word_timing(backwards) is False

    def test_line_level_and_empty_files_are_not_flagged(self):
        assert lyrics_module.has_crowded_word_timing(
            "[00:10.00]Just a line\n[00:14.00]And another") is False
        assert lyrics_module.has_crowded_word_timing("") is False
        assert lyrics_module.has_crowded_word_timing(None) is False

    def test_a_single_word_line_cannot_be_crowded(self):
        assert lyrics_module.has_crowded_word_timing(
            "[00:10.00]<00:10.00>Oooh") is False

    def test_the_real_shape_of_the_defect(self):
        """The line that prompted this, verbatim from a produced file."""
        crammed = ("[02:21.70]<02:21.70>Don't <02:22.37>stop <02:22.97>(don't "
                   "<02:23.31>stop, <02:23.51>stop <02:23.74>what <02:23.97>you're "
                   "<02:24.39>doing) <02:25.15>doin' <02:25.16>what <02:25.18>you're "
                   "<02:25.19>doin'")
        assert lyrics_module.has_crowded_word_timing(crammed) is True
        # And it looks perfectly healthy to everything else.
        assert lyrics_module.has_word_timing(crammed) is True
        assert lyrics_module.has_backwards_word_timing(crammed) is False
        assert lyrics_module.looks_synthetic(crammed) is False
