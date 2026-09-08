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
