"""Backfill synced lyrics for tracks already in the library.

Walks the music library, finds audio files that have no .lrc sidecar yet,
reads their tags, and fetches synced lyrics through the same
LRCLIB/Musixmatch pipeline a fresh grab uses. Best effort per track: a
missing match or a network error is counted and skipped, never fatal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import mutagen

from .config import Config
from .library import write_lyrics_sidecar
from .lyrics import fetch_synced_lyrics

# Audio we tag/file; the video library's .mp4 files are ignored.
AUDIO_EXTS = (".opus", ".ogg", ".mp3", ".m4a", ".flac")
# A small pause between lookups keeps the lyric providers happy on a big
# library scan.
REQUEST_SPACING = 0.2


def _noop(*args) -> None:
    pass


@dataclass
class BackfillResult:
    total: int      # audio files found without a .lrc sidecar
    added: int      # sidecars written
    skipped: int    # files with no usable artist/title tags
    no_match: int   # looked up but no synced lyrics found


def iter_audio_missing_lyrics(root: Path):
    """Every audio file under root that has no .lrc sidecar yet, in a
    stable order."""
    for path in sorted(root.rglob("*")):
        if (path.is_file() and path.suffix.lower() in AUDIO_EXTS
                and not path.with_suffix(".lrc").exists()):
            yield path


def read_track_meta(path: Path):
    """(artist, title, album, duration_seconds) from a file's tags, or None
    if it cannot be read. Missing text fields come back as ""."""
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception:
        return None
    if audio is None:
        return None

    def first(key: str) -> str:
        value = audio.get(key)
        if isinstance(value, list):
            return value[0] if value else ""
        return value or ""

    duration = None
    if getattr(audio, "info", None) is not None and audio.info.length:
        duration = int(audio.info.length)
    return (first("artist"), first("title"), first("album"), duration)


def backfill_lyrics(
    config: Config,
    on_progress: Callable[[float], None] = _noop,
    on_detail: Callable[[str], None] = _noop,
    files: Optional[list] = None,
) -> BackfillResult:
    """Fetch and write .lrc sidecars for library tracks missing them.

    on_progress/on_detail let the job layer mirror the scan and also act as
    cancellation checkpoints. `files` lets a caller pre-compute the list.
    """
    if files is None:
        files = list(iter_audio_missing_lyrics(config.music_root))
    total = len(files)
    added = skipped = no_match = 0

    for index, path in enumerate(files):
        meta = read_track_meta(path)
        if not meta or not (meta[0] and meta[1]):
            skipped += 1
        else:
            artist, title, album, duration = meta
            try:
                lrc = fetch_synced_lyrics(
                    artist, title, album, duration,
                    musixmatch_token=config.musixmatch_token,
                    provider=config.lyrics_provider)
            except Exception:
                lrc = None
            if lrc:
                try:
                    write_lyrics_sidecar(path, lrc)
                    added += 1
                except Exception:
                    no_match += 1  # write failed; treat as not added
            else:
                no_match += 1
            time.sleep(REQUEST_SPACING)
        on_detail("%d/%d checked, %d lyrics added" % (index + 1, total, added))
        if total:
            on_progress((index + 1) / total * 100.0)

    return BackfillResult(total=total, added=added, skipped=skipped,
                          no_match=no_match)
