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

        def fake_fetch(artist, title, album="", duration_seconds=None,
                       musixmatch_token="", provider="lrclib"):
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

        def fake_fetch(artist, title, album="", duration_seconds=None,
                       musixmatch_token="", provider="lrclib"):
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
