"""Library-wide synced-lyrics backfill: the file walk, tag reading, the
fetch/write loop, and the scan endpoint. Network faked."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import beetdrop.backfill as backfill
from beetdrop.app import create_app
from beetdrop.backfill import (
    backfill_lyrics,
    iter_audio_line_level_lyrics,
    iter_audio_missing_lyrics,
    meta_from_path,
    read_track_meta,
)
from beetdrop.config import Config
from beetdrop.db import Store
from beetdrop.fulltags import FullTags, write_full_tags


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def make_opus(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", str(path), "-y", "-loglevel", "error"],
        check=True, timeout=60)


class TestIterMissing:
    def test_finds_audio_without_lrc_only(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "song.opus").write_bytes(b"x")
        (tmp_path / "a" / "has.opus").write_bytes(b"x")
        (tmp_path / "a" / "has.lrc").write_text("[00:01.00]hi")  # already has one
        (tmp_path / "a" / "video.mp4").write_bytes(b"x")         # not audio
        (tmp_path / "a" / "cover.jpg").write_bytes(b"x")         # not audio

        found = [p.name for p in iter_audio_missing_lyrics(tmp_path)]
        assert found == ["song.opus"]


class TestMetaFromPath:
    def test_clean_layout(self, tmp_path):
        p = tmp_path / "AmaLee" / "My Ninja Way (2019)" / "02 - Go.opus"
        assert meta_from_path(p, tmp_path) == ("AmaLee", "Go", "My Ninja Way")

    def test_disc_prefix_stripped(self, tmp_path):
        p = tmp_path / "A" / "Album" / "1-04 - Song.mp3"
        assert meta_from_path(p, tmp_path) == ("A", "Song", "Album")

    def test_review_folder(self, tmp_path):
        p = tmp_path / "_review" / "AmaLee - Go" / "Go.opus"
        assert meta_from_path(p, tmp_path) == ("AmaLee", "Go", "")

    def test_flat_artist_dash_title(self, tmp_path):
        p = tmp_path / "AmaLee - Go.opus"
        assert meta_from_path(p, tmp_path) == ("AmaLee", "Go", "")

    def test_artist_folder_only(self, tmp_path):
        p = tmp_path / "AmaLee" / "Go.opus"
        assert meta_from_path(p, tmp_path) == ("AmaLee", "Go", "")


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestReadMeta:
    def test_reads_tags_and_duration(self, tmp_path):
        path = tmp_path / "t.opus"
        make_opus(path)
        write_full_tags(path, FullTags(title="Go", artist="AmaLee",
                                       album="My Ninja Way"))
        artist, title, album, duration = read_track_meta(path)
        assert (artist, title, album) == ("AmaLee", "Go", "My Ninja Way")
        assert duration and duration >= 1

    def test_untagged_returns_blanks(self, tmp_path):
        path = tmp_path / "t.opus"
        make_opus(path)
        meta = read_track_meta(path)
        assert meta is not None
        assert meta[0] == "" and meta[1] == ""  # no artist/title


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestBackfill:
    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_writes_sidecars_and_counts(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        # One tagged track that has lyrics, one tagged with none, one untagged.
        good = config.music_root / "AmaLee" / "hit.opus"
        make_opus(good)
        write_full_tags(good, FullTags(title="HasLyrics", artist="AmaLee"))
        dry = config.music_root / "AmaLee" / "dry.opus"
        make_opus(dry)
        write_full_tags(dry, FullTags(title="NoLyrics", artist="AmaLee"))
        # Untagged and directly under the root with no "Artist - Title"
        # name: nothing can be derived, so it is genuinely skipped.
        bare = config.music_root / "bare.opus"
        make_opus(bare)

        def fake_fetch(artist, title, album="", duration_seconds=None, **kwargs):
            return "[00:01.00]la" if title == "HasLyrics" else None
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake_fetch)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        result = backfill_lyrics(config)
        assert result.total == 3
        assert result.added == 1
        assert result.no_match == 1
        assert result.skipped == 1
        assert good.with_suffix(".lrc").read_text() == "[00:01.00]la"
        assert not dry.with_suffix(".lrc").exists()

    def test_untagged_file_uses_path_and_duration(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        # No embedded tags at all - artist/title must come from the layout,
        # and the duration must still come from the decoded audio.
        track = config.music_root / "AmaLee" / "My Ninja Way (2019)" / "02 - Go.opus"
        make_opus(track)

        seen = {}

        def fake_fetch(artist, title, album="", duration_seconds=None, **kwargs):
            seen.update(artist=artist, title=title, album=album,
                        duration=duration_seconds)
            return "[00:01.00]la"
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake_fetch)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        result = backfill_lyrics(config)
        assert result.added == 1 and result.skipped == 0
        assert seen["artist"] == "AmaLee"
        assert seen["title"] == "Go"
        assert seen["album"] == "My Ninja Way"
        assert seen["duration"] and seen["duration"] >= 1  # from the audio
        assert track.with_suffix(".lrc").read_text() == "[00:01.00]la"

    def test_existing_lrc_is_skipped(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        track = config.music_root / "A" / "x.opus"
        make_opus(track)
        write_full_tags(track, FullTags(title="HasLyrics", artist="A"))
        track.with_suffix(".lrc").write_text("[00:00.00]already")

        called = {"n": 0}

        def fake_fetch(*a, **k):
            called["n"] += 1
            return "[00:01.00]new"
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake_fetch)

        result = backfill_lyrics(config)
        # The track already had a sidecar, so it was never looked up or touched.
        assert result.total == 0 and called["n"] == 0
        assert track.with_suffix(".lrc").read_text() == "[00:00.00]already"


SYNTHETIC = "\n".join(
    "[%02d:%02d.00]Wob gopini den %d" % ((12 + i * 4) // 60, (12 + i * 4) % 60, i)
    for i in range(40))
# Real lyrics: irregular line timing, as any human transcription has.
REAL = ("[00:11.20]Thought I found a way\n[00:14.05]Thought I found a way out\n"
        "[00:19.90]But you never go away\n[00:21.00]So I guess I gotta stay now\n"
        "[00:30.40]Oh, I hope some day I'll make it out of here\n"
        "[00:37.10]Even if it takes all night or a hundred years\n"
        "[00:44.00]Need a place to hide but I can't find one near\n"
        "[00:52.75]Wanna feel alive, outside I can fight my fear\n"
        "[01:03.10]Isn't it lovely, all alone?\n[01:09.44]Heart made of glass\n")


class TestSyntheticDetection:
    def test_uniform_timing_is_flagged(self):
        from beetdrop.lyrics import looks_synthetic
        assert looks_synthetic(SYNTHETIC) is True

    def test_real_lyrics_pass(self):
        from beetdrop.lyrics import looks_synthetic
        assert looks_synthetic(REAL) is False

    def test_short_lyrics_never_flagged(self):
        from beetdrop.lyrics import looks_synthetic
        short = "[00:01.00]a\n[00:05.00]b\n[00:09.00]c"
        assert looks_synthetic(short) is False

    def test_synthetic_never_returned_by_fetch(self, monkeypatch):
        import beetdrop.lyrics as lyrics_module
        monkeypatch.setattr(lyrics_module, "_lrclib",
                            lambda a, t, al, d: SYNTHETIC)
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            lambda *a, **k: None)
        # A source handing back placeholder text yields nothing at all.
        assert lyrics_module.fetch_synced_lyrics("Billie Eilish", "lovely") is None

    def test_falls_back_when_primary_is_synthetic(self, monkeypatch):
        import beetdrop.lyrics as lyrics_module
        monkeypatch.setattr(lyrics_module, "_lrclib",
                            lambda a, t, al, d: SYNTHETIC)
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            lambda *a, **k: REAL)
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", musixmatch_token="tok") == REAL


class TestPurge:
    def test_purges_only_placeholder_files(self, tmp_path):
        from beetdrop.backfill import find_bad_lyrics, purge_bad_lyrics
        (tmp_path / "A").mkdir()
        bad = tmp_path / "A" / "junk.lrc"
        bad.write_text(SYNTHETIC)
        good = tmp_path / "A" / "real.lrc"
        good.write_text(REAL)

        assert [p.name for p in find_bad_lyrics(tmp_path)] == ["junk.lrc"]
        assert purge_bad_lyrics(tmp_path) == 1
        assert not bad.exists()
        assert good.read_text() == REAL  # real lyrics untouched

    def test_refresh_purges_then_refetches(self, tmp_path, monkeypatch):
        import beetdrop.backfill as bf
        music = tmp_path / "music"
        (music / "A").mkdir(parents=True)
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        track = music / "A" / "song.opus"
        track.write_bytes(b"x")  # tags unreadable; path fallback supplies A/song
        track.with_suffix(".lrc").write_text(SYNTHETIC)

        monkeypatch.setattr(bf, "fetch_synced_lyrics",
                            lambda *a, **k: REAL)
        monkeypatch.setattr(bf, "REQUEST_SPACING", 0)
        result = bf.backfill_lyrics(config, purge_bad=True)
        assert result.purged == 1
        assert result.added == 1
        assert track.with_suffix(".lrc").read_text() == REAL


class TestScanEndpoint:
    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_scan_starts_job(self, tmp_path):
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            resp = client.post("/api/lyrics/scan")
        assert resp.status_code == 202
        body = resp.json()
        assert body["kind"] == "lyricscan"
        assert body["title"] == "Library lyrics scan"

    def test_scan_conflicts_when_already_running(self, tmp_path):
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            # A scan already in flight (created after startup so it is not
            # swept as interrupted) blocks a second one.
            store = Store(config.db_path)
            job = store.create_job("__library__", "opus", "192", kind="lyricscan")
            store.update_job(job["id"], stage="scanning")
            resp = client.post("/api/lyrics/scan")
        assert resp.status_code == 409


WORD_LRC = "[00:09.26]<00:09.26>I <00:09.64>drove <00:10.00>by"
LINE_LRC = "[00:09.26]I drove by\n[00:12.10]We used to hang out"


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestUpgradePass:
    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def _track(self, config, name, lrc=None, title="HasLyrics"):
        path = config.music_root / "A" / ("%s.opus" % name)
        make_opus(path)
        write_full_tags(path, FullTags(title=title, artist="A"))
        if lrc is not None:
            path.with_suffix(".lrc").write_text(lrc)
        return path

    def test_finds_only_line_level_sidecars(self, tmp_path):
        config = self._config(tmp_path)
        line = self._track(config, "line", LINE_LRC)
        self._track(config, "word", WORD_LRC)   # already word-level
        self._track(config, "none", None)       # no sidecar at all
        found = list(iter_audio_line_level_lyrics(config.music_root))
        assert found == [line]

    def test_replaces_line_level_with_word_level(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        track = self._track(config, "line", LINE_LRC)
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: WORD_LRC)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config, upgrade=True)
        assert result.total == 1 and result.upgraded == 1
        assert track.with_suffix(".lrc").read_text() == WORD_LRC

    def test_keeps_existing_when_no_word_version_exists(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        track = self._track(config, "line", LINE_LRC)
        # Apple only has line timing for this one - the existing file must
        # survive untouched rather than be overwritten with no improvement.
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: "[00:01.00]some other line lyrics")
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config, upgrade=True)
        assert result.upgraded == 0 and result.no_match == 1
        assert track.with_suffix(".lrc").read_text() == LINE_LRC

    def test_nothing_found_returns_none_and_keeps_file(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        track = self._track(config, "line", LINE_LRC)
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", lambda *a, **k: None)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config, upgrade=True)
        assert result.upgraded == 0
        assert track.with_suffix(".lrc").read_text() == LINE_LRC

    def test_upgrade_always_asks_for_word_timing(self, tmp_path, monkeypatch):
        """Even with the standing setting off - the run is an explicit ask."""
        config = self._config(tmp_path)
        config.word_lyrics = False
        self._track(config, "line", LINE_LRC)
        seen = {}

        def fake(*a, **k):
            seen.update(k)
            return WORD_LRC
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        backfill_lyrics(config, upgrade=True)
        assert seen["word_by_word"] is True

    def test_normal_scan_still_never_overwrites(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        track = self._track(config, "line", LINE_LRC)
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: WORD_LRC)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config)  # not an upgrade run
        assert result.total == 0
        assert track.with_suffix(".lrc").read_text() == LINE_LRC


class TestTidyTrackName:
    """Files from other downloaders carry no artist tag and the whole
    "Artist - Title (Audio)" string as the title. Searching a catalogue
    with that finds nothing."""

    @pytest.mark.parametrize("title,artist,expected", [
        ("5 Seconds of Summer  - Social Casualty (Audio)",
         "5 Seconds of Summer", "Social Casualty"),
        ("Adele - Hello", "Adele", "Hello"),
        ("lovely (Official Video)", "Billie Eilish", "lovely"),
        ("Social Casualty", "5 Seconds of Summer", "Social Casualty"),
        ("This Nearly Was Mine", "Frank Sinatra", "This Nearly Was Mine"),
    ])
    def test_cleans_junk_titles(self, title, artist, expected):
        assert backfill.tidy_track_name(title, artist) == expected

    def test_keeps_a_real_qualifier(self):
        # "- Live" is part of the recording's identity, not an artist prefix.
        assert backfill.tidy_track_name("Live and Let Die - Live", "Wings") \
            == "Live and Let Die - Live"

    def test_does_not_strip_a_different_artist(self):
        # Only the track's own artist is stripped, never a collaborator
        # or a song whose title happens to contain a dash.
        assert backfill.tidy_track_name("Nirvana - Something", "Adele") \
            == "Nirvana - Something"

    def test_empty_and_untidyable(self):
        assert backfill.tidy_track_name("", "A") == ""
        assert backfill.tidy_track_name("(Audio)", "A") == "(Audio)"


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestJunkTagsReachTheLookupClean:
    def test_artist_is_not_duplicated_into_the_query(self, tmp_path, monkeypatch):
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        # Exactly the shape another downloader leaves: no artist tag, the
        # whole "Artist - Title (Audio)" crammed into the title.
        track = music / "5 Seconds of Summer" / "5 Seconds of Summer  - Social Casualty.opus"
        make_opus(track)
        write_full_tags(track, FullTags(
            title="5 Seconds of Summer  - Social Casualty (Audio)", artist=""))

        seen = {}

        def fake(artist, title, album="", duration_seconds=None, **kwargs):
            seen.update(artist=artist, title=title)
            return "[00:01.00]la"
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        backfill_lyrics(config)
        assert seen["artist"] == "5 Seconds of Summer"
        assert seen["title"] == "Social Casualty"
        # The artist must not appear twice in what a catalogue is asked for.
        assert seen["title"].count("5 Seconds of Summer") == 0


class TestLyricsStats:
    """The "Check library" report: read-only counting, nothing fetched."""

    def _library(self, tmp_path):
        root = tmp_path / "music"
        (root / "A").mkdir(parents=True)
        return root

    def test_counts_each_kind(self, tmp_path):
        root = self._library(tmp_path)
        word = root / "A" / "word.opus"
        word.write_bytes(b"x")
        word.with_suffix(".lrc").write_text(
            "[00:09.26]<00:09.26>I <00:09.64>drove")
        line = root / "A" / "line.opus"
        line.write_bytes(b"x")
        line.with_suffix(".lrc").write_text("[00:09.26]I drove\n[00:14.00]away")
        (root / "A" / "none.opus").write_bytes(b"x")
        (root / "A" / "cover.jpg").write_bytes(b"x")   # not audio
        (root / "A" / "clip.mp4").write_bytes(b"x")    # video library

        stats = backfill.lyrics_stats(root)
        assert stats.audio_files == 3
        assert stats.with_lyrics == 2
        assert stats.word_level == 1
        assert stats.line_level == 1
        assert stats.missing == 1
        assert stats.placeholder == 0
        assert round(stats.coverage_pct) == 67
        assert round(stats.word_pct) == 50

    def test_counts_placeholder_junk(self, tmp_path):
        root = self._library(tmp_path)
        junk = root / "A" / "junk.opus"
        junk.write_bytes(b"x")
        junk.with_suffix(".lrc").write_text(SYNTHETIC)
        stats = backfill.lyrics_stats(root)
        assert stats.placeholder == 1
        assert stats.line_level == 1  # junk is line-level too

    def test_empty_library_does_not_divide_by_zero(self, tmp_path):
        stats = backfill.lyrics_stats(self._library(tmp_path))
        assert stats.audio_files == 0
        assert stats.coverage_pct == 0.0 and stats.word_pct == 0.0

    def test_endpoint_reports_the_counts(self, tmp_path):
        music = tmp_path / "music"
        (music / "A").mkdir(parents=True)
        track = music / "A" / "t.opus"
        track.write_bytes(b"x")
        track.with_suffix(".lrc").write_text("[00:01.00]<00:01.00>hi")
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            body = client.get("/api/lyrics/stats").json()
        assert body["audio_files"] == 1
        assert body["word_level"] == 1
        assert body["coverage_pct"] == 100.0
        assert body["word_pct"] == 100.0


class TestUpgradeRepairsBackwardsTiming:
    def test_a_backwards_word_file_is_offered_to_the_upgrade(self, tmp_path):
        root = tmp_path / "music"
        (root / "A").mkdir(parents=True)
        broken = root / "A" / "broken.opus"
        broken.write_bytes(b"x")
        broken.with_suffix(".lrc").write_text(
            "[02:40.85]<02:40.85>It's <02:41.25>so <02:41.80>cold "
            "<02:40.85>(Out he-e-ere)")
        sound = root / "A" / "sound.opus"
        sound.write_bytes(b"x")
        sound.with_suffix(".lrc").write_text(
            "[02:40.85]<02:40.85>It <02:41.25>is <02:41.80>cold")

        found = [p.name for p in iter_audio_line_level_lyrics(root)]
        assert found == ["broken.opus"]  # the sound one is left alone


class TestStatsNameTheTracks:
    def _library(self, tmp_path):
        root = tmp_path / "music"
        (root / "A").mkdir(parents=True)
        return root

    def test_each_bucket_is_named(self, tmp_path):
        root = self._library(tmp_path)
        for name, lrc in [("word", "[00:01.00]<00:01.00>hi"),
                          ("line", "[00:01.00]plain"),
                          ("none", None)]:
            (root / "A" / (name + ".opus")).write_bytes(b"x")
            if lrc:
                (root / "A" / (name + ".lrc")).write_text(lrc)
        stats = backfill.lyrics_stats(root)
        assert stats.line_level_files == ["A/line.opus"]
        assert stats.missing_files == ["A/none.opus"]
        # A sound word-level track is in no bucket.
        assert "A/word.opus" not in stats.line_level_files

    def test_backwards_word_timing_is_called_out(self, tmp_path):
        """A sidecar whose word tags rewind counts as word-level - it does
        carry per-word timing - but it is broken, so the report has to say
        so rather than let it pass as a win. Real shape, from a library
        filed before the background-vocal fix."""
        root = self._library(tmp_path)
        broken = root / "A" / "broken.opus"
        broken.write_bytes(b"x")
        broken.with_suffix(".lrc").write_text(
            "[01:00.42]<01:00.42>Understand <01:01.71>me "
            "<01:00.42>(Understand me)")
        sound = root / "A" / "sound.opus"
        sound.write_bytes(b"x")
        sound.with_suffix(".lrc").write_text(
            "[00:01.00]<00:01.00>Un <00:01.40>der <00:01.90>stand")

        stats = backfill.lyrics_stats(root)
        assert stats.word_level == 2          # both do have word timing
        assert stats.backwards == 1           # but one of them rewinds
        assert stats.backwards_files == ["A/broken.opus"]
        assert stats.line_level == 0

    def test_sample_caps_the_lists(self, tmp_path):
        root = self._library(tmp_path)
        for i in range(10):
            track = root / "A" / ("t%d.opus" % i)
            track.write_bytes(b"x")
            track.with_suffix(".lrc").write_text("[00:01.00]plain")
        capped = backfill.lyrics_stats(root, sample=3)
        assert capped.line_level == 10          # the count is complete
        assert len(capped.line_level_files) == 3  # the list is capped
        every = backfill.lyrics_stats(root, sample=0)
        assert len(every.line_level_files) == 10

    def test_endpoint_includes_the_names(self, tmp_path):
        root = self._library(tmp_path)
        track = root / "A" / "line.opus"
        track.write_bytes(b"x")
        track.with_suffix(".lrc").write_text("[00:01.00]plain")
        config = Config(music_root=root, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            body = client.get("/api/lyrics/stats").json()
        assert body["line_level_files"] == ["A/line.opus"]


class TestUpgradeAsksOnlyApple:
    """The upgrade pass keeps a result only when it has per-word timing,
    and Apple is the only source of that. Asking the others is round trips
    spent on an answer that is thrown away."""

    def _count(self, **kwargs):
        import time
        from unittest import mock

        import requests

        from beetdrop import apple, lyrics
        seen = []

        class Miss:
            ok = False
            status_code = 404
            text = ""

            def json(self):
                return {}

        apple._dev_token["value"] = "dev"   # pretend already cached
        apple._dev_token["at"] = time.time()

        def counting_get(url, **kw):
            seen.append(url.split("/")[2])
            return Miss()

        with mock.patch.object(requests, "get", counting_get):
            lyrics.fetch_synced_lyrics("Adele", "Hello (Live)", "25", 295,
                                       apple_token="t", musixmatch_token="m",
                                       **kwargs)
        return seen

    def test_word_only_skips_the_sources_that_cannot_answer(self):
        hosts = self._count(word_by_word=True, word_only=True)
        assert all("apple" in h for h in hosts), hosts

    def test_without_word_only_the_whole_chain_is_walked(self):
        hosts = self._count(word_by_word=True)
        assert any("lrclib" in h for h in hosts)
        assert any("musixmatch" in h for h in hosts)


class TestUnreachableIsNotAMiss:
    """The bug this closes: every failure - a rate limit, a rotated token,
    a dropped connection - arrived at the scan as the same None a track
    with genuinely no lyrics produces. A run with the network down
    recorded the whole library as "no match" and reported success."""

    def _config(self, tmp_path):
        music = tmp_path / "music"
        (music / "A").mkdir(parents=True)
        track = music / "A" / "song.opus"
        track.write_bytes(b"x")
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c"), track

    def _run(self, tmp_path, monkeypatch, outcome):
        config, track = self._config(tmp_path)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))

        def fake(*a, **k):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        return backfill_lyrics(config), track

    def test_unreachable_counts_as_deferred_not_no_match(self, tmp_path, monkeypatch):
        from beetdrop.lyrics import LyricsUnavailable
        result, track = self._run(tmp_path, monkeypatch,
                                  LyricsUnavailable("lrclib returned 503"))
        assert result.deferred == 1
        assert result.no_match == 0      # it was never answered
        assert result.added == 0
        assert not track.with_suffix(".lrc").exists()

    def test_a_real_miss_is_still_a_miss(self, tmp_path, monkeypatch):
        result, _ = self._run(tmp_path, monkeypatch, None)
        assert (result.no_match, result.deferred) == (1, 0)

    def test_the_run_says_it_was_incomplete(self, tmp_path, monkeypatch):
        from beetdrop.lyrics import LyricsUnavailable
        config, _ = self._config(tmp_path)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))

        def boom(*a, **k):
            raise LyricsUnavailable("Apple returned 429")
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", boom)
        said = []
        backfill_lyrics(config, on_detail=said.append)
        assert any("deferred" in line for line in said), said
        assert any("429" in line for line in said), said


class TestChainDefersOnlyWhenEmptyHanded:
    def _stub(self, monkeypatch, lrclib=None, apple_exc=None):
        from beetdrop import lyrics as ly
        monkeypatch.setattr(ly, "_lrclib", lambda *a, **k: lrclib)
        monkeypatch.setattr(ly, "_musixmatch", lambda *a, **k: None)

        def fake_apple(*a, **k):
            if apple_exc:
                raise apple_exc
            return None
        monkeypatch.setattr(ly, "_apple", fake_apple)
        return ly

    def test_a_hit_wins_over_an_unreachable_source(self, monkeypatch):
        """One source being down does not matter if another answered."""
        from beetdrop.lyrics import LyricsUnavailable
        ly = self._stub(monkeypatch, lrclib="[00:01.00]found",
                        apple_exc=LyricsUnavailable("Apple 429"))
        got = ly.fetch_synced_lyrics("A", "S", apple_token="t",
                                     word_by_word=True)
        assert got == "[00:01.00]found"

    def test_empty_handed_with_a_source_down_defers(self, monkeypatch):
        from beetdrop.lyrics import LyricsUnavailable
        ly = self._stub(monkeypatch, lrclib=None,
                        apple_exc=LyricsUnavailable("Apple 429"))
        with pytest.raises(LyricsUnavailable):
            ly.fetch_synced_lyrics("A", "S", apple_token="t")

    def test_empty_handed_with_every_source_answering_is_a_miss(self, monkeypatch):
        ly = self._stub(monkeypatch, lrclib=None)
        assert ly.fetch_synced_lyrics("A", "S", apple_token="t") is None

    def test_word_only_defers_when_apple_is_down(self, monkeypatch):
        """The upgrade pass asks only Apple, so Apple being down leaves it
        with no answer at all - that must not read as "no word lyrics"."""
        from beetdrop.lyrics import LyricsUnavailable
        ly = self._stub(monkeypatch, apple_exc=LyricsUnavailable("Apple 429"))
        with pytest.raises(LyricsUnavailable):
            ly.fetch_synced_lyrics("A", "S", apple_token="t",
                                   word_by_word=True, word_only=True)


class TestUpgradeRefusesABadReplacement:
    """The upgrade pass overwrites an existing sidecar, so what it accepts
    has to be strictly better. Word timing that rewinds mid-line is not:
    replacing sound line-level lyrics with it is a regression, and calling
    it an upgrade reports a repair that never happened."""

    BACKWARDS = "[01:00.42]<01:00.42>Understand <01:01.71>me <01:00.42>(Understand me)"
    GOOD = "[00:01.00]<00:01.00>Un <00:01.40>der <00:01.90>stand"

    def _run(self, tmp_path, monkeypatch, existing, answer):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        (config.music_root / "A").mkdir(parents=True)
        track = config.music_root / "A" / "song.opus"
        track.write_bytes(b"x")
        track.with_suffix(".lrc").write_text(existing)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: answer)
        return backfill_lyrics(config, upgrade=True), track

    def test_backwards_answer_is_refused(self, tmp_path, monkeypatch):
        result, track = self._run(tmp_path, monkeypatch,
                                  "[00:01.00]plain line", self.BACKWARDS)
        assert result.upgraded == 0
        assert result.no_match == 1
        # The sound line-level sidecar is still there, not overwritten.
        assert track.with_suffix(".lrc").read_text() == "[00:01.00]plain line"

    def test_a_good_answer_is_still_taken(self, tmp_path, monkeypatch):
        result, track = self._run(tmp_path, monkeypatch,
                                  "[00:01.00]plain line", self.GOOD)
        assert result.upgraded == 1
        assert track.with_suffix(".lrc").read_text() == self.GOOD

    def test_a_broken_file_is_not_repaired_with_another_broken_one(
            self, tmp_path, monkeypatch):
        result, track = self._run(tmp_path, monkeypatch,
                                  self.BACKWARDS, self.BACKWARDS)
        assert result.upgraded == 0          # nothing was actually fixed
        assert track.with_suffix(".lrc").read_text() == self.BACKWARDS


class TestTrackNumberPrefix:
    """Ripped albums name files "06 Title.m4a" with no punctuation after
    the number. The prefix pattern required a "-" or ".", so those were
    searched for as "06 Chance To Love You More" - which no catalogue
    has, making every such track a permanent miss."""

    @pytest.mark.parametrize("stem,expected", [
        ("06 I'll Give It All", "I'll Give It All"),
        ("14 Chance To Love You More", "Chance To Love You More"),
        ("11 Free", "Free"),
        ("2-04 When There Was Me And You", "When There Was Me And You"),
        ("1-02 - Song", "Song"),
        ("02 - Song", "Song"),
        ("02. Song", "Song"),
        ("100 Song", "Song"),
    ])
    def test_prefix_is_stripped(self, tmp_path, stem, expected):
        p = tmp_path / "Artist" / "Album" / (stem + ".opus")
        assert meta_from_path(p, tmp_path)[1] == expected

    @pytest.mark.parametrize("stem", [
        "1999",             # the whole name is the number
        "24K Magic",        # digits run into the word
        "Song 2",           # number is not leading
    ])
    def test_a_real_title_is_left_alone(self, tmp_path, stem):
        p = tmp_path / "Artist" / "Album" / (stem + ".opus")
        assert meta_from_path(p, tmp_path)[1] == stem


class TestArtistPrefixWithoutADash:
    """Rips are often named "<Artist> <Title>" or "<Artist>-<Title>" with
    no " - " separator, and the strip only handled the spaced dash - so
    "Adele I Found A Boy" was searched for under Adele as "Adele I Found
    A Boy"."""

    @pytest.mark.parametrize("title,artist,expected", [
        ("Adele I Found A Boy", "Adele", "I Found A Boy"),
        ("Blue October The Still", "Blue October", "The Still"),
        ("Blue October-Conversation Via Radio", "Blue October",
         "Conversation Via Radio"),
        ("5 Seconds of Summer - Social Casualty", "5 Seconds of Summer",
         "Social Casualty"),
    ])
    def test_prefix_removed(self, title, artist, expected):
        assert backfill.tidy_track_name(title, artist) == expected

    def test_a_title_that_is_only_the_artist_survives(self):
        # Bon Jovi's "Bon Jovi": stripping everything would leave nothing
        # to search for, so the title is kept whole.
        assert backfill.tidy_track_name("Bon Jovi", "Bon Jovi") == "Bon Jovi"

    def test_an_unrelated_leading_word_is_kept(self):
        assert backfill.tidy_track_name("Yesterday", "The Beatles") == "Yesterday"

    def test_with_lyrics_suffix_is_noise(self):
        assert backfill.tidy_track_name(
            "Conversation Via Radio (with lyrics)", "Blue October"
        ) == "Conversation Via Radio"


class TestScanButtonsPickTheRightPass:
    """Each button has to reach the pass it names.

    The Upgrade button posted a plain scan: the UI accepted an `upgrade`
    argument and then never put it in the URL, so pressing it silently
    ran the fetch-missing pass. The server understood the flag the whole
    time, which is why nothing here caught it - so pin the markers, and
    check the page actually sends them.
    """

    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    @pytest.mark.parametrize("query,marker", [
        ("", "__library__"),
        ("?refresh=true", "__refresh__"),
        ("?upgrade=true", "__upgrade__"),
        # Upgrade wins, matching the server's own precedence.
        ("?refresh=true&upgrade=true", "__upgrade__"),
    ])
    def test_marker_matches_the_requested_pass(self, tmp_path, query, marker):
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            body = client.post("/api/lyrics/scan" + query).json()
        assert body["video_id"] == marker

    def test_the_page_sends_the_upgrade_flag(self):
        """Guards the wiring itself: the bug was entirely in the page."""
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        scan = source[source.index("async scanLyrics("):]
        scan = scan[:scan.index("async appleSignIn(")]
        assert "upgrade=true" in scan, "the Upgrade button posts a plain scan"
        assert "refresh=true" in scan


class TestWordCoverageEstimate:
    """Answering "is an upgrade pass worth running?" by running it costs
    two Apple calls per track and hours of rate-limited waiting. A random
    sample answers it in minutes, so long as the margin is honest."""

    def _library(self, tmp_path, count):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        for i in range(count):
            track = config.music_root / "A" / ("t%03d.opus" % i)
            track.write_bytes(b"x")
            track.with_suffix(".lrc").write_text("[00:01.00]plain")
        return config

    def test_samples_rather_than_checking_everything(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, 200)
        asked = []
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", p.stem, "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def fake(*a, **k):
            asked.append(1)
            return "[00:01.00]<00:01.00>hi"
        monkeypatch.setattr("beetdrop.lyrics.fetch_synced_lyrics", fake)

        est = backfill.estimate_word_coverage(config, sample=20, seed=1)
        assert len(asked) == 20            # not all 200
        assert est.population == 200
        assert est.pct == 100.0
        assert est.projected == 200        # scaled back up to the population

    def test_half_and_half_lands_near_fifty(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, 200)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", p.stem, "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        seen = {"n": 0}

        def fake(*a, **k):
            seen["n"] += 1
            return "[00:01.00]<00:01.00>hi" if seen["n"] % 2 else "[00:01.00]plain"
        monkeypatch.setattr("beetdrop.lyrics.fetch_synced_lyrics", fake)

        est = backfill.estimate_word_coverage(config, sample=100, seed=7)
        assert 45 <= est.pct <= 55
        assert 0 < est.margin < 15          # a real interval, not a guess

    def test_rate_limited_tracks_are_excluded_not_counted_as_no(
            self, tmp_path, monkeypatch):
        """Counting a deferred track as "Apple has nothing" would drag the
        estimate down precisely when Apple is refusing to answer."""
        from beetdrop.lyrics import LyricsUnavailable
        config = self._library(tmp_path, 50)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", p.stem, "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            if calls["n"] % 2:
                raise LyricsUnavailable("Apple 429")
            return "[00:01.00]<00:01.00>hi"
        monkeypatch.setattr("beetdrop.lyrics.fetch_synced_lyrics", fake)

        est = backfill.estimate_word_coverage(config, sample=20, seed=3)
        assert est.deferred == 10
        assert est.sampled == 10
        assert est.pct == 100.0        # of what was actually answered

    def test_an_empty_library_is_not_an_error(self, tmp_path):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        config.music_root.mkdir(parents=True)
        est = backfill.estimate_word_coverage(config)
        assert est.population == 0 and est.pct == 0.0

    def test_nothing_is_written(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, 10)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", p.stem, "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr("beetdrop.lyrics.fetch_synced_lyrics",
                            lambda *a, **k: "[00:01.00]<00:01.00>hi")
        backfill.estimate_word_coverage(config, sample=10, seed=1)
        for lrc in (config.music_root / "A").glob("*.lrc"):
            assert lrc.read_text() == "[00:01.00]plain"   # untouched
