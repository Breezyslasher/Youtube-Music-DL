"""Library-wide synced-lyrics backfill: the file walk, tag reading, the
fetch/write loop, and the scan endpoint. Network faked."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import beetdrop.backfill as backfill
from beetdrop.lyrics import strip_source
from beetdrop.app import create_app
from beetdrop.backfill import (
    backfill_lyrics,
    iter_audio_line_level_lyrics,
    iter_audio_missing_lyrics,
    iter_audio_word_level_lyrics,
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
        assert strip_source(lyrics_module.fetch_synced_lyrics(
            "A", "S", musixmatch_token="tok")) == REAL


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


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestRedoWordsPass:
    """Apple times syllables, and we used to put a space between every
    one, so "Tumble" was written "Tum ble". A finished .lrc no longer
    says where the word breaks were, so no test of the file can find the
    spoiled ones - the repair pass has to revisit them all."""

    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def _track(self, config, name, lrc=None):
        path = config.music_root / "A" / ("%s.opus" % name)
        make_opus(path)
        write_full_tags(path, FullTags(title=name, artist="A"))
        if lrc is not None:
            path.with_suffix(".lrc").write_text(lrc)
        return path

    def test_finds_every_word_level_sidecar(self, tmp_path):
        config = self._config(tmp_path)
        word = self._track(config, "word", WORD_LRC)
        self._track(config, "line", LINE_LRC)
        self._track(config, "none", None)
        assert list(iter_audio_word_level_lyrics(config.music_root)) == [word]

    def test_a_sound_word_level_file_is_still_revisited(self, tmp_path,
                                                        monkeypatch):
        # The upgrade pass deliberately skips these; the repair pass must
        # not, because "sound" is exactly what it cannot tell.
        config = self._config(tmp_path)
        self._track(config, "word", WORD_LRC)
        assert list(iter_audio_line_level_lyrics(config.music_root)) == []
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: WORD_LRC)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config, redo_words=True)
        assert result.total == 1 and result.upgraded == 1

    def test_rewrites_the_sidecar_with_what_apple_returns_now(self, tmp_path,
                                                              monkeypatch):
        config = self._config(tmp_path)
        split = "[00:01.00]<00:01.00>Tum <00:01.50>ble <00:02.00>out"
        joined = "[00:01.00]<00:01.00>Tum<00:01.50>ble <00:02.00>out"
        track = self._track(config, "word", split)
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: joined)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config, redo_words=True)
        assert result.upgraded == 1
        assert track.with_suffix(".lrc").read_text() == joined

    def test_a_line_level_answer_never_overwrites_word_timing(self, tmp_path,
                                                              monkeypatch):
        config = self._config(tmp_path)
        track = self._track(config, "word", WORD_LRC)
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: LINE_LRC)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        result = backfill_lyrics(config, redo_words=True)
        assert result.upgraded == 0 and result.no_match == 1
        assert track.with_suffix(".lrc").read_text() == WORD_LRC

    def test_nothing_found_leaves_the_file_alone(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        track = self._track(config, "word", WORD_LRC)
        monkeypatch.setattr(backfill, "fetch_synced_lyrics",
                            lambda *a, **k: None)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        backfill_lyrics(config, redo_words=True)
        assert track.with_suffix(".lrc").read_text() == WORD_LRC

    def test_it_asks_apple_alone_for_word_timing(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        config.word_lyrics = False
        self._track(config, "word", WORD_LRC)
        seen = {}

        def fake(*args, **kwargs):
            seen.update(kwargs)
            return WORD_LRC

        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        backfill_lyrics(config, redo_words=True)
        assert seen["word_by_word"] is True and seen["word_only"] is True


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
        assert strip_source(got) == "[00:01.00]found"

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
        ("?redo_words=true", "__rewords__"),
        # Upgrade wins, matching the server's own precedence.
        ("?refresh=true&upgrade=true", "__upgrade__"),
        ("?upgrade=true&redo_words=true", "__rewords__"),
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
        assert "redo_words=true" in scan

    def test_the_page_offers_the_re_render_button(self):
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        assert "scanLyrics(false, false, true)" in page


class TestReviewQueueEndpoints:
    """The review queue is a whole workflow with no test behind it, and
    the page it drives has already shipped two wiring bugs."""

    def _client(self, tmp_path):
        music = tmp_path / "m"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        return TestClient(create_app(config)), config

    def test_an_empty_queue_reports_nothing(self, tmp_path):
        client, _ = self._client(tmp_path)
        with client:
            body = client.get("/api/lyrics/reviews").json()
        assert body == {"reviews": [], "total": 0}

    def test_clearing_an_empty_queue_removes_nothing(self, tmp_path):
        client, _ = self._client(tmp_path)
        with client:
            body = client.delete("/api/lyrics/reviews").json()
        assert body == {"removed": 0}

    def test_clearing_empties_a_queue_that_had_entries(self, tmp_path,
                                                       monkeypatch):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        (config.music_root / "A").mkdir(parents=True)
        track = config.music_root / "A" / "song.opus"
        track.write_bytes(b"x")
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def refuse(*a, **k):
            if k.get("on_candidates"):
                k["on_candidates"]([{"id": "1", "title": "Song (Live)",
                                     "artist": "A", "reason": "live"}])
            return None

        monkeypatch.setattr(backfill, "fetch_synced_lyrics", refuse)
        app = create_app(config)
        with TestClient(app) as client:
            # Same database the app opened, so the API sees what a scan wrote.
            backfill_lyrics(config, store=Store(config.db_path))
            assert client.get("/api/lyrics/reviews").json()["total"] == 1
            assert client.delete("/api/lyrics/reviews").json() == {"removed": 1}
            assert client.get("/api/lyrics/reviews").json()["total"] == 0

    def test_the_page_offers_the_clear_button(self):
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        assert 'click="clearReviews"' in page
        assert "clearReviews()" in source
        assert '"DELETE"' in source


class TestTheQueueDoesNotGoStale:
    """The queue is keyed by path and was only ever emptied by deciding a
    track. Anything that failed a *different* way on a later run, and
    anything whose file was deleted, stayed in the list for good - asking
    for decisions that no longer applied."""

    def _config(self, tmp_path):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        track = config.music_root / "A" / "song.opus"
        track.write_bytes(b"x")
        return config, track

    def _run(self, config, store, monkeypatch, candidates=None, lrc=None):
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def fake(*a, **k):
            if candidates and k.get("on_candidates"):
                k["on_candidates"](candidates)
            return lrc

        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        return backfill_lyrics(config, store=store)

    REFUSED = [{"id": "1", "title": "Song (Live)", "artist": "A",
                "reason": "a different performance"}]

    def test_a_track_apple_now_offers_nothing_for_is_forgotten(
            self, tmp_path, monkeypatch):
        config, track = self._config(tmp_path)
        store = Store(config.db_path)
        self._run(config, store, monkeypatch, candidates=self.REFUSED)
        assert store.count_reviews() == 1
        # Second run: Apple returns nothing at all this time.
        self._run(config, store, monkeypatch, candidates=None)
        assert store.count_reviews() == 0

    def test_a_still_refused_track_is_refreshed_not_duplicated(
            self, tmp_path, monkeypatch):
        config, _ = self._config(tmp_path)
        store = Store(config.db_path)
        self._run(config, store, monkeypatch, candidates=self.REFUSED)
        self._run(config, store, monkeypatch, candidates=self.REFUSED)
        assert store.count_reviews() == 1

    def test_a_track_that_finally_gets_lyrics_leaves_the_queue(
            self, tmp_path, monkeypatch):
        config, track = self._config(tmp_path)
        store = Store(config.db_path)
        self._run(config, store, monkeypatch, candidates=self.REFUSED)
        assert store.count_reviews() == 1
        track.with_suffix(".lrc").unlink(missing_ok=True)
        self._run(config, store, monkeypatch, lrc="[00:01.00]found at last")
        assert store.count_reviews() == 0

    def test_a_queued_track_whose_file_is_gone_is_forgotten(self, tmp_path,
                                                            monkeypatch):
        config, track = self._config(tmp_path)
        store = Store(config.db_path)
        self._run(config, store, monkeypatch, candidates=self.REFUSED)
        assert store.count_reviews() == 1
        track.unlink()
        self._run(config, store, monkeypatch)
        assert store.count_reviews() == 0

    def test_a_file_that_still_exists_is_kept(self, tmp_path):
        config, track = self._config(tmp_path)
        store = Store(config.db_path)
        store.add_review(str(track), "A", "Song", 200, [{"id": "1"}])
        assert store.drop_missing_reviews() == 0
        assert store.count_reviews() == 1

    def test_a_deferred_track_keeps_its_entry(self, tmp_path, monkeypatch):
        # Nobody could answer, so nothing was learned about the track -
        # dropping it would throw away a decision still worth making.
        config, _ = self._config(tmp_path)
        store = Store(config.db_path)
        self._run(config, store, monkeypatch, candidates=self.REFUSED)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def unavailable(*a, **k):
            raise backfill.LyricsUnavailable("rate limited")

        monkeypatch.setattr(backfill, "fetch_synced_lyrics", unavailable)
        backfill_lyrics(config, store=store)
        assert store.count_reviews() == 1


class TestSearchingForAMissingTrackByHand:
    """A track Apple returned nothing for never reaches the review queue -
    there is nothing to choose between - so until now there was no way to
    intervene on it at all. Usually the tags are the reason, and a person
    can see that where matching cannot."""

    def _config(self, tmp_path):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        return config

    def _track(self, config, name="Aladdin.opus", lrc=None):
        path = config.music_root / "A" / name
        path.write_bytes(b"x")
        if lrc is not None:
            path.with_suffix(".lrc").write_text(lrc)
        return path

    def test_it_lists_only_tracks_with_no_lyrics(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        missing = self._track(config, "missing.opus")
        self._track(config, "has.opus", "[00:01.00]la")
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Orchestra", "Aladdin", "Al", 200))
        with TestClient(create_app(config)) as client:
            body = client.get("/api/lyrics/unmatched").json()
        assert [t["path"] for t in body["tracks"]] == [str(missing)]
        assert body["total"] == 1

    def test_a_listed_track_carries_what_we_would_have_searched_for(
            self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        self._track(config, "missing.opus")
        monkeypatch.setattr(
            backfill, "read_track_meta",
            lambda p: ("Orchestra", "Orchestra - Aladdin (Audio)", "Al", 200))
        with TestClient(create_app(config)) as client:
            track = client.get("/api/lyrics/unmatched").json()["tracks"][0]
        # Tidied exactly as the scan tidies it, so what a person is handed
        # to edit is the query that actually failed - not a cleaner one
        # that would hide why nothing was found.
        assert track["artist"] == "Orchestra" and track["title"] == "Aladdin"
        assert track["duration"] == 200

    def test_the_listing_is_capped_but_the_total_is_not(self, tmp_path,
                                                        monkeypatch):
        config = self._config(tmp_path)
        for i in range(5):
            self._track(config, "m%d.opus" % i)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "T", "Al", 1))
        with TestClient(create_app(config)) as client:
            body = client.get("/api/lyrics/unmatched?limit=2").json()
        assert len(body["tracks"]) == 2 and body["total"] == 5

    def test_search_returns_candidates_unfiltered(self, tmp_path, monkeypatch):
        from beetdrop import apple
        # No relevance check here on purpose: the track whose tags defeated
        # matching is the one where matching's opinion is worth least.
        config = self._config(tmp_path)
        monkeypatch.setattr(apple, "fetch_developer_token", lambda *a, **k: "d")
        monkeypatch.setattr(apple, "search_catalog", lambda *a, **k: [
            {"id": "1", "attributes": {"name": "A Whole New World",
                                       "artistName": "Peabo Bryson",
                                       "albumName": "Aladdin",
                                       "durationInMillis": 240000,
                                       "releaseDate": "1992-01-01"}}])
        with TestClient(create_app(config)) as client:
            body = client.get("/api/lyrics/search?q=aladdin").json()
        assert body["results"][0]["title"] == "A Whole New World"
        assert body["results"][0]["duration"] == 240

    def test_an_empty_query_asks_apple_nothing(self, tmp_path, monkeypatch):
        from beetdrop import apple
        config = self._config(tmp_path)
        monkeypatch.setattr(apple, "fetch_developer_token", lambda *a, **k:
                            pytest.fail("searched on an empty query"))
        with TestClient(create_app(config)) as client:
            assert client.get("/api/lyrics/search?q=%20").json() == {"results": []}

    def test_apple_being_unreachable_is_reported_not_swallowed(
            self, tmp_path, monkeypatch):
        from beetdrop import apple
        config = self._config(tmp_path)

        def boom(*a, **k):
            raise apple.AppleError("no developer token")

        monkeypatch.setattr(apple, "fetch_developer_token", boom)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/lyrics/search?q=aladdin").status_code == 502

    def test_the_page_wires_the_search_up(self):
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        assert 'click="loadUnmatched"' in page
        assert "searchLyricsFor(track)" in page
        assert "useSearchResult(track, song.id)" in page
        for method in ("loadUnmatched()", "searchLyricsFor(", "useSearchResult("):
            assert method in source, method
        assert "/api/lyrics/unmatched" in source
        assert "/api/lyrics/search?q=" in source


class TestTaggingWhatAlreadyExists:
    """Establishing the source of sidecars written before the tag.

    Offline, and it must never guess: a plausible source written into a
    file is read as a fact by everything after it.
    """

    WORDS = "[00:01.00]<00:01.00>Tumble <00:01.40>out\n[00:05.00]<00:05.00>And\n"
    LINES = "[00:01.00]Tumble out of bed\n[00:05.00]And stumble\n"

    def _library(self, tmp_path):
        root = tmp_path / "music"
        album = root / "Dolly Parton" / "9 to 5 (1980)"
        album.mkdir(parents=True)
        for n in (1, 2, 3):
            (album / ("%02d - T.opus" % n)).write_bytes(b"x")
        (album / "01 - T.lrc").write_text(self.WORDS)   # only Apple can do this
        (album / "02 - T.lrc").write_text(self.LINES)   # any of the three
        return root

    def test_word_level_is_established_as_apple(self, tmp_path):
        from beetdrop.backfill import tag_existing_sources
        from beetdrop.lyrics import lyric_provenance

        root = self._library(tmp_path)
        found = tag_existing_sources(root)
        assert found.tagged == 1 and found.unknowable == 1
        tagged = (root / "Dolly Parton" / "9 to 5 (1980)" / "01 - T.lrc")
        assert lyric_provenance(tagged.read_text()).source == "apple"

    def test_line_level_is_left_alone_rather_than_guessed(self, tmp_path):
        from beetdrop.backfill import tag_existing_sources
        from beetdrop.lyrics import lyric_provenance

        root = self._library(tmp_path)
        untouched = root / "Dolly Parton" / "9 to 5 (1980)" / "02 - T.lrc"
        before = untouched.read_text()
        tag_existing_sources(root)
        assert untouched.read_text() == before
        assert lyric_provenance(untouched.read_text()).source == ""

    def test_the_build_is_recorded_as_unknown(self, tmp_path):
        # Claiming this build rendered a file it did not is the one lie
        # that would matter: the version is what a repair pass reads to
        # decide whether a file needs redoing.
        from beetdrop.backfill import tag_existing_sources
        from beetdrop.lyrics import UNKNOWN_VERSION, lyric_provenance

        root = self._library(tmp_path)
        tag_existing_sources(root)
        tagged = root / "Dolly Parton" / "9 to 5 (1980)" / "01 - T.lrc"
        assert lyric_provenance(tagged.read_text()).version == UNKNOWN_VERSION

    def test_the_lyrics_are_not_touched(self, tmp_path):
        from beetdrop.backfill import tag_existing_sources
        from beetdrop.lyrics import strip_source

        root = self._library(tmp_path)
        tag_existing_sources(root)
        tagged = root / "Dolly Parton" / "9 to 5 (1980)" / "01 - T.lrc"
        assert strip_source(tagged.read_text()) == self.WORDS

    def test_an_existing_tag_is_left_as_it_is(self, tmp_path):
        from beetdrop.backfill import tag_existing_sources
        from beetdrop.lyrics import lyric_provenance, stamp_source

        root = self._library(tmp_path)
        already = root / "Dolly Parton" / "9 to 5 (1980)" / "03 - T.lrc"
        already.write_text(stamp_source(self.WORDS, "lrclib"))
        found = tag_existing_sources(root)
        assert found.already == 1
        # Not overwritten with the inference: what the file says beats
        # what could be worked out about it.
        assert lyric_provenance(already.read_text()).source == "lrclib"

    def test_running_it_twice_changes_nothing_the_second_time(self, tmp_path):
        from beetdrop.backfill import tag_existing_sources

        root = self._library(tmp_path)
        tag_existing_sources(root)
        second = tag_existing_sources(root)
        assert second.tagged == 0 and second.already == 1

    def test_the_endpoint_reports_both_halves(self, tmp_path):
        root = self._library(tmp_path)
        config = Config(music_root=root, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            body = client.post("/api/lyrics/tag-sources").json()
        assert body["tagged"] == 1
        assert body["unknowable"] == 1
        assert body["total"] == 2


class TestTheLibraryAndStatsEndpoints:
    """Library and Stats are walks of the tree, not a cached index: at a
    real library size the walk plus a read of every sidecar is under a
    second, and a second copy of the truth is a thing that can be wrong."""

    def _library(self, tmp_path):
        root = tmp_path / "music"
        album = root / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
        album.mkdir(parents=True)
        for n in (1, 2, 4):        # a gap at 3
            (album / ("%02d - Track.opus" % n)).write_bytes(b"x")
        (album / "01 - Track.lrc").write_text("[00:01.00]<00:01.00>word")
        (album / "02 - Track.lrc").write_text("[00:01.00]line only")
        review = root / "_review" / "Unknown - Some Song"
        review.mkdir(parents=True)
        (review / "track.opus").write_bytes(b"x")
        return Config(music_root=root, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_albums_carry_what_the_screen_shows(self, tmp_path):
        config = self._library(tmp_path)
        with TestClient(create_app(config)) as client:
            body = client.get("/api/library?sort=az").json()
        assert body["total"] == 2
        album = [a for a in body["items"] if a["artist"] == "Dolly Parton"][0]
        assert album["track_count"] == 3
        # From the numbering, not MusicBrainz: 04 exists, 03 does not.
        assert album["expected_count"] == 4 and album["incomplete"] is True
        assert album["lyrics"] == {"word": 1, "line": 1, "junk": 0, "none": 1}
        assert album["format"] == "opus" and album["verified"] is True

    def test_stats_break_the_sidecars_down_by_source(self, tmp_path):
        """Which source wrote each .lrc, which nothing recorded before.

        Without it, "1,861 line-level" cannot be turned into "how many
        does Apple have no words for", and a repair pass has no way to
        leave the good files alone.
        """
        from beetdrop.lyrics import stamp_source

        config = self._library(tmp_path)
        album = config.music_root / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
        (album / "01 - Track.lrc").write_text(
            stamp_source("[00:01.00]<00:01.00>word", "apple"))
        with TestClient(create_app(config)) as client:
            stats = client.get("/api/stats").json()
        counted = {row["source"]: row["count"]
                   for row in stats["lyrics"]["by_source"]}
        assert counted["apple"] == 1
        # The other sidecar predates the tag. Untagged means unrecorded,
        # never "not Apple", so it is counted apart rather than guessed.
        assert counted["untagged"] == 1

    def test_a_track_row_says_who_wrote_its_sidecar(self, tmp_path):
        from beetdrop.lyrics import stamp_source

        config = self._library(tmp_path)
        album = config.music_root / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
        (album / "02 - Track.lrc").write_text(
            stamp_source("[00:01.00]line only", "lrclib"))
        with TestClient(create_app(config)) as client:
            listing = client.get("/api/library?sort=az").json()
            ident = [a for a in listing["items"]
                     if a["artist"] == "Dolly Parton"][0]["id"]
            tracks = client.get("/api/library/album/%s" % ident).json()["tracks"]
        by_name = {row["name"]: row for row in tracks}
        assert by_name["02 - Track.opus"]["lyrics_source"] == "lrclib"
        assert by_name["01 - Track.opus"]["lyrics_source"] == ""

    def test_a_review_grab_is_its_own_unverified_album(self, tmp_path):
        config = self._library(tmp_path)
        with TestClient(create_app(config)) as client:
            body = client.get("/api/library?filter=unverified").json()
        assert body["total"] == 1
        assert body["items"][0]["verified"] is False
        assert body["items"][0]["artist"] == "Unknown"

    @pytest.mark.parametrize("name,expected", [
        ("missing_lyrics", 2), ("line_only", 1), ("unverified", 1),
        ("incomplete", 1), ("junk", 0),
    ])
    def test_each_chip_counts_what_it_filters(self, tmp_path, name, expected):
        config = self._library(tmp_path)
        with TestClient(create_app(config)) as client:
            counts = client.get("/api/library/counts").json()["counts"]
            listed = client.get("/api/library?filter=%s" % name).json()["total"]
        assert counts[name] == expected, name
        assert listed == expected, "%s lists a different number than it counts" % name

    def test_an_album_lists_its_tracks_with_per_track_lyrics(self, tmp_path):
        config = self._library(tmp_path)
        with TestClient(create_app(config)) as client:
            found = client.get("/api/library?filter=incomplete").json()["items"][0]
            tracks = client.get("/api/library/album/%s" % found["id"]).json()["tracks"]
        assert [t["number"] for t in tracks] == [1, 2, 4]
        assert [t["lyrics"] for t in tracks] == ["word", "line", "none"]

    def test_an_id_pointing_outside_the_library_is_refused(self, tmp_path):
        # The id is a reversible encoding of a path, so it has to be held
        # to the library like every other path arriving over HTTP.
        import base64
        config = self._library(tmp_path)
        escape = base64.urlsafe_b64encode(b"../../etc").decode().rstrip("=")
        with TestClient(create_app(config)) as client:
            assert client.get("/api/library/album/%s" % escape).status_code == 404

    def test_stats_totals_the_same_library(self, tmp_path):
        config = self._library(tmp_path)
        with TestClient(create_app(config)) as client:
            stats = client.get("/api/stats").json()
        assert stats["tracks"] == 4 and stats["albums"] == 2
        assert stats["incomplete_albums"] == 1
        assert stats["review_count"] == 1
        assert {key: stats["lyrics"][key]
                for key in ("word", "line", "junk", "none", "bad_timing")} \
            == {"word": 1, "line": 1, "junk": 0, "none": 2, "bad_timing": 0}
        assert 74 < stats["verified_pct"] < 76      # 3 of 4 verified

    def test_stats_says_when_the_job_history_is_short(self, tmp_path):
        # A chart that quietly loses its early days reads as a drop in
        # activity that did not happen.
        config = self._library(tmp_path)
        with TestClient(create_app(config)) as client:
            reliability = client.get("/api/stats").json()["reliability"]
        assert reliability["history_days"] == 14
        assert len(reliability["activity"]) == 14
        assert reliability["history_capped"] is False   # no grabs at all


class TestWordCoverageEstimate:
    """Answering "is an upgrade pass worth running?" by running it costs
    two Apple calls per track and hours of waiting. A random sample
    answers it in minutes - so long as it separates "Apple has no word
    timing" from "nothing we searched for matched", which are different
    problems with different fixes."""

    def _library(self, tmp_path, count):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        for i in range(count):
            track = config.music_root / "A" / ("t%03d.opus" % i)
            track.write_bytes(b"x")
            track.with_suffix(".lrc").write_text("[00:01.00]plain")
        return config

    def _apple(self, monkeypatch, rows, word=True):
        """rows(path_index) -> a catalog row, or None for no match."""
        from beetdrop import apple
        asked = []
        monkeypatch.setattr(apple, "fetch_developer_token", lambda force=False: "d")
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", p.stem, "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def search(dev, sf, artist, title, duration=None):
            asked.append(title)
            return rows(len(asked) - 1)
        monkeypatch.setattr(apple, "search_song_row", search)
        monkeypatch.setattr(apple, "fetch_ttml",
                            lambda *a, **k: "<tt/>")
        monkeypatch.setattr(apple, "is_word_level", lambda ttml: word)
        return asked

    def test_samples_rather_than_checking_everything(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, 200)
        asked = self._apple(monkeypatch, lambda i: {"id": "1", "attributes": {}})
        est = backfill.estimate_word_coverage(config, sample=20, seed=1)
        assert len(asked) == 20            # not all 200
        assert est.population == 200
        assert est.matched == 20 and est.pct == 100.0
        assert est.projected == 200        # scaled back up to the population

    def test_a_failed_match_is_not_blamed_on_apple(self, tmp_path, monkeypatch):
        """The bug this closes: a track Apple never found was counted the
        same as one Apple has no word timing for, so rough tags read as
        "Apple does not have it"."""
        config = self._library(tmp_path, 100)
        self._apple(monkeypatch,
                    lambda i: None if i % 2 else {"id": "1", "attributes": {}})
        est = backfill.estimate_word_coverage(config, sample=20, seed=2)
        assert est.not_found == 10
        assert est.matched == 10
        assert est.pct == 100.0        # of what Apple actually recognised
        assert est.pct_of_all == 50.0  # of everything asked about
        assert est.match_pct == 50.0
        assert est.not_found_files       # named, so they can be eyeballed

    def test_matched_but_no_word_timing_is_apples_answer(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, 100)
        self._apple(monkeypatch, lambda i: {"id": "1", "attributes": {}},
                    word=False)
        est = backfill.estimate_word_coverage(config, sample=10, seed=3)
        assert est.matched == 10 and est.no_word == 10
        assert est.not_found == 0 and est.pct == 0.0

    def test_a_no_lyrics_flag_saves_the_second_call(self, tmp_path, monkeypatch):
        from beetdrop import apple
        config = self._library(tmp_path, 20)
        self._apple(monkeypatch, lambda i: {
            "id": "1", "attributes": {"hasTimeSyncedLyrics": False}})
        fetched = []
        monkeypatch.setattr(apple, "fetch_ttml",
                            lambda *a, **k: fetched.append(1))
        est = backfill.estimate_word_coverage(config, sample=10, seed=4)
        assert est.matched == 10 and est.no_word == 10
        assert not fetched          # Apple already said there are none

    def test_rate_limited_tracks_are_excluded_not_counted_as_no(
            self, tmp_path, monkeypatch):
        """Counting a deferred track as "Apple has nothing" would drag the
        estimate down precisely when Apple is refusing to answer."""
        from beetdrop import apple
        config = self._library(tmp_path, 50)
        calls = {"n": 0}

        def search(dev, sf, artist, title, duration=None):
            calls["n"] += 1
            if calls["n"] % 2:
                raise apple.AppleUnavailable("429")
            return {"id": "1", "attributes": {}}
        self._apple(monkeypatch, lambda i: None)
        monkeypatch.setattr(apple, "search_song_row", search)
        est = backfill.estimate_word_coverage(config, sample=20, seed=3)
        assert est.deferred == 10
        assert est.matched == 10
        assert est.pct == 100.0        # of what was actually answered

    def test_an_empty_library_is_not_an_error(self, tmp_path):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        config.music_root.mkdir(parents=True)
        est = backfill.estimate_word_coverage(config)
        assert est.population == 0 and est.pct == 0.0

    def test_nothing_is_written(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, 10)
        self._apple(monkeypatch, lambda i: {"id": "1", "attributes": {}})
        backfill.estimate_word_coverage(config, sample=10, seed=1)
        for lrc in (config.music_root / "A").glob("*.lrc"):
            assert lrc.read_text() == "[00:01.00]plain"   # untouched


class TestIsrcReading:
    """An ISRC names one exact recording, so a lookup by it cannot return
    the wrong version the way a text search can. Every format spells the
    tag differently and MP4 has no standard atom at all, so the reader
    has to know all of them or it reports a library as having none."""

    def _flac(self, tmp_path, isrc):
        pytest.importorskip("mutagen.flac")
        import subprocess
        path = tmp_path / "t.flac"
        subprocess.run(["ffmpeg", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", str(path),
                        "-y", "-loglevel", "error"], check=True, timeout=60)
        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        audio["ISRC"] = isrc
        audio.save()
        return path

    @pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
    def test_reads_a_vorbis_isrc(self, tmp_path):
        path = self._flac(tmp_path, "GBAYE0601498")
        assert backfill.read_isrc(path) == "GBAYE0601498"

    @pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
    def test_dashes_and_case_are_normalised(self, tmp_path):
        # Taggers write "gb-aye-06-01498"; Apple wants the bare form.
        path = self._flac(tmp_path, "gb-aye-06-01498")
        assert backfill.read_isrc(path) == "GBAYE0601498"

    @pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
    def test_no_isrc_is_empty_not_an_error(self, tmp_path):
        import subprocess
        path = tmp_path / "bare.flac"
        subprocess.run(["ffmpeg", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", str(path),
                        "-y", "-loglevel", "error"], check=True, timeout=60)
        assert backfill.read_isrc(path) == ""

    def test_an_unreadable_file_is_empty_not_an_error(self, tmp_path):
        path = tmp_path / "junk.mp3"
        path.write_bytes(b"not audio")
        assert backfill.read_isrc(path) == ""

    @pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
    def test_coverage_counts_across_the_library(self, tmp_path):
        root = tmp_path / "m"
        root.mkdir()
        tagged = self._flac(tmp_path, "GBAYE0601498")
        import shutil
        shutil.copy(tagged, root / "one.flac")
        shutil.copy(tagged, root / "two.flac")
        (root / "three.opus").write_bytes(b"x")     # unreadable, no ISRC
        with_isrc, checked = backfill.isrc_coverage(root)
        assert (with_isrc, checked) == (2, 3)


class TestVerifySkipFlag:
    """fetch_synced skips the lyrics request when Apple's search says a
    track has no synced lyrics. That rests on the flag being accurate on
    an anonymous search - assumed from one working example, never
    established. This check makes the skipped request anyway."""

    def _library(self, tmp_path, count=20):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        for i in range(count):
            track = config.music_root / "A" / ("t%02d.opus" % i)
            track.write_bytes(b"x")
            track.with_suffix(".lrc").write_text("[00:01.00]plain")
        return config

    def _stub(self, monkeypatch, flagged_false, ttml):
        from beetdrop import apple
        monkeypatch.setattr(apple, "fetch_developer_token", lambda force=False: "d")
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", p.stem, "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr(apple, "search_song_row", lambda *a, **k: {
            "id": "1",
            "attributes": {"hasTimeSyncedLyrics": not flagged_false}})
        monkeypatch.setattr(apple, "fetch_ttml", lambda *a, **k: ttml)
        monkeypatch.setattr(apple, "is_word_level", lambda t: "Word" in (t or ""))

    def test_a_lying_flag_is_caught(self, monkeypatch, tmp_path):
        config = self._library(tmp_path)
        self._stub(monkeypatch, flagged_false=True, ttml="<tt Word/>")
        check = backfill.verify_skip_flag(config, sample=5)
        assert check.flagged_false == 5
        assert check.had_lyrics == 5 and check.had_word == 5
        assert check.flag_is_wrong
        assert check.wrong_examples

    def test_an_honest_flag_clears_the_skip(self, monkeypatch, tmp_path):
        config = self._library(tmp_path)
        self._stub(monkeypatch, flagged_false=True, ttml=None)
        check = backfill.verify_skip_flag(config, sample=5)
        assert check.flagged_false == 5
        assert check.had_lyrics == 0
        assert not check.flag_is_wrong

    def test_tracks_apple_never_flags_are_not_counted(self, monkeypatch, tmp_path):
        config = self._library(tmp_path)
        self._stub(monkeypatch, flagged_false=False, ttml="<tt Word/>")
        check = backfill.verify_skip_flag(config, sample=5, max_searches=10)
        # Nothing was flagged, so the check has nothing to say either way.
        assert check.flagged_false == 0 and check.had_lyrics == 0
        assert not check.flag_is_wrong

    def test_the_search_budget_is_bounded(self, monkeypatch, tmp_path):
        config = self._library(tmp_path, count=50)
        self._stub(monkeypatch, flagged_false=False, ttml=None)
        check = backfill.verify_skip_flag(config, sample=30, max_searches=7)
        assert check.searched <= 7

    def test_nothing_is_written(self, monkeypatch, tmp_path):
        config = self._library(tmp_path)
        self._stub(monkeypatch, flagged_false=True, ttml="<tt Word/>")
        backfill.verify_skip_flag(config, sample=5)
        for lrc in (config.music_root / "A").glob("*.lrc"):
            assert lrc.read_text() == "[00:01.00]plain"


class TestRefusedMatchesAreQueuedForReview:
    """Once matching started turning candidates away, a track with no
    lyrics could mean Apple had nothing or that we declined everything it
    offered. Only the second is worth a person's attention, so only that
    is queued - listing tracks with nothing to choose between would be
    clicking through blanks."""

    def _config(self, tmp_path):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        track = config.music_root / "A" / "song.opus"
        track.write_bytes(b"x")
        return config, track

    def _run(self, tmp_path, monkeypatch, candidates=None, store=None):
        config, track = self._config(tmp_path)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def fake(*a, **k):
            if candidates and k.get("on_candidates"):
                k["on_candidates"](candidates)
            return None
        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        return backfill_lyrics(config, store=store), track

    def test_refused_candidates_are_queued(self, tmp_path, monkeypatch):
        store = Store(tmp_path / "db.sqlite3")
        refused = [{"id": "1", "title": "Song (Live)", "artist": "A",
                    "reason": "a different performance"}]
        result, track = self._run(tmp_path, monkeypatch, refused, store)
        assert result.no_match == 1
        assert store.count_reviews() == 1
        row = store.list_reviews()[0]
        assert row["path"] == str(track)
        assert row["candidates"][0]["reason"] == "a different performance"

    def test_a_track_apple_had_nothing_for_is_not_queued(self, tmp_path, monkeypatch):
        store = Store(tmp_path / "db.sqlite3")
        result, _ = self._run(tmp_path, monkeypatch, candidates=None, store=store)
        assert result.no_match == 1
        assert store.count_reviews() == 0     # nothing to choose between

    def test_a_scan_without_a_store_still_works(self, tmp_path, monkeypatch):
        result, _ = self._run(tmp_path, monkeypatch, [{"id": "1"}], store=None)
        assert result.no_match == 1

    def _run_redo(self, tmp_path, monkeypatch, candidates, store, answer=None):
        """A re-render pass over a track that already has a word-level
        sidecar, where matching refuses everything Apple offers."""
        config, track = self._config(tmp_path)
        track.with_suffix(".lrc").write_text(WORD_LRC)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def fake(*a, **k):
            if candidates and k.get("on_candidates"):
                k["on_candidates"](candidates)
            return answer

        monkeypatch.setattr(backfill, "fetch_synced_lyrics", fake)
        return backfill_lyrics(config, redo_words=True, store=store), track

    def test_a_re_render_that_kept_the_old_file_is_queued_too(self, tmp_path,
                                                              monkeypatch):
        # The pass leaves the sidecar alone and reports it under "left as
        # they were", which does not say whether Apple had nothing or we
        # refused what it had. On a repair pass that is the difference
        # between a fixed file and one still spoiled.
        store = Store(tmp_path / "db.sqlite3")
        refused = [{"id": "1", "title": "Song (Live)", "artist": "A",
                    "reason": "a different performance"}]
        result, track = self._run_redo(tmp_path, monkeypatch, refused, store)
        assert result.upgraded == 0 and result.no_match == 1
        assert track.with_suffix(".lrc").read_text() == WORD_LRC
        assert store.count_reviews() == 1
        assert store.list_reviews()[0]["path"] == str(track)

    def test_no_word_version_is_not_a_refusal(self, tmp_path, monkeypatch):
        # Apple matched the track and simply has no word timing for it.
        # Nobody can overrule that, so it must not join the queue.
        store = Store(tmp_path / "db.sqlite3")
        result, _ = self._run_redo(tmp_path, monkeypatch, None, store,
                                   answer=LINE_LRC)
        assert result.no_match == 1
        assert store.count_reviews() == 0

    def test_a_successful_re_render_queues_nothing(self, tmp_path, monkeypatch):
        store = Store(tmp_path / "db.sqlite3")
        result, track = self._run_redo(tmp_path, monkeypatch, None, store,
                                       answer=WORD_LRC)
        assert result.upgraded == 1
        assert store.count_reviews() == 0


class TestAChoiceOutranksMatching:
    """A decision is durable on purpose: a rescan must never quietly undo
    it, and correcting a wrong automatic match has to stick."""

    def _config(self, tmp_path):
        config = Config(music_root=tmp_path / "m", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c", apple_token="t")
        (config.music_root / "A").mkdir(parents=True)
        track = config.music_root / "A" / "song.opus"
        track.write_bytes(b"x")
        return config, track

    def test_a_chosen_song_is_used_and_matching_is_skipped(self, tmp_path, monkeypatch):
        config, track = self._config(tmp_path)
        store = Store(tmp_path / "db.sqlite3")
        store.set_choice(str(track), "1369380479")
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr(
            backfill, "fetch_synced_lyrics",
            lambda *a, **k: pytest.fail("matching ran despite a decision"))
        asked = []
        monkeypatch.setattr(backfill, "_lyrics_by_choice",
                            lambda cfg, song_id, word_by_word=False:
                            asked.append(song_id) or "[00:01.00]picked")

        result = backfill_lyrics(config, store=store)
        assert asked == ["1369380479"]
        assert result.added == 1
        assert track.with_suffix(".lrc").read_text() == "[00:01.00]picked"

    def test_leave_alone_is_honoured(self, tmp_path, monkeypatch):
        config, track = self._config(tmp_path)
        store = Store(tmp_path / "db.sqlite3")
        store.set_choice(str(track), "")      # "none of these"
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("A", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr(
            backfill, "fetch_synced_lyrics",
            lambda *a, **k: pytest.fail("looked up a track marked leave-alone"))

        result = backfill_lyrics(config, store=store)
        assert result.skipped == 1
        assert not track.with_suffix(".lrc").exists()


class TestPlaceholderForAnInstrumental:
    """A source with nothing to say for an instrumental answers with a
    marker rather than a refusal. Written out it is a sidecar carrying no
    lyrics that still makes the track look done, so no later pass
    revisits it. Found on a real library: "Instrumental", a lone dash, and
    a bare timestamp, on Eruption and other instrumentals."""

    from beetdrop.lyrics import carries_no_lyrics as _check

    @pytest.mark.parametrize("lrc", [
        "[00:00.00]Instrumental",
        "[00:00.00] Instrumental",
        "[00:00.00]",
        "[00:00.00]-",
        "[00:01.00]Intro\n[00:05.00]Outro",
        "",
    ])
    def test_rejected(self, lrc):
        assert backfill.carries_no_lyrics(lrc)

    @pytest.mark.parametrize("lrc", [
        "[00:01.60]Be a good boy and put this on",     # a real one-line skit
        "[00:00.23]Are you still up?",
        "[00:01.00]<00:01.00>real <00:01.40>words",
        "[00:01.00]Instrumental break coming up now",  # real words, not a marker
    ])
    def test_kept(self, lrc):
        assert not backfill.carries_no_lyrics(lrc)

    def test_such_a_file_is_swept_up_by_refresh(self, tmp_path):
        root = tmp_path / "m"
        (root / "A").mkdir(parents=True)
        (root / "A" / "eruption.lrc").write_text("[00:00.00]Instrumental")
        (root / "A" / "real.lrc").write_text(
            "[00:11.20]Thought I found a way\n[00:14.05]out")
        bad = [p.name for p in backfill.find_bad_lyrics(root)]
        assert bad == ["eruption.lrc"]

    def test_it_is_never_written_in_the_first_place(self, monkeypatch):
        from beetdrop import lyrics as ly
        monkeypatch.setattr(ly, "_lrclib", lambda *a, **k: "[00:00.00]Instrumental")
        monkeypatch.setattr(ly, "_musixmatch", lambda *a, **k: None)
        monkeypatch.setattr(ly, "_apple", lambda *a, **k: None)
        assert ly.fetch_synced_lyrics("A", "S") is None
