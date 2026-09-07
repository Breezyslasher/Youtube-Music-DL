"""Backfill synced lyrics for tracks already in the library.

Walks the music library, finds audio files that have no .lrc sidecar yet,
reads their tags, and fetches synced lyrics through the same
LRCLIB/Musixmatch pipeline a fresh grab uses. Best effort per track: a
missing match or a network error is counted and skipped, never fatal.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import mutagen

from .config import Config
from .library import write_lyrics_sidecar
from .lyrics import fetch_synced_lyrics

# Leading track (and disc) number on a filename, e.g. "02 - ", "1-02 - ".
_TRACK_PREFIX = re.compile(r"^(?:\d+-)?\d+\s*[-.]\s*")
# Trailing " (1999)" year on an album folder.
_YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")

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


def meta_from_path(path: Path, root: Path):
    """Best-effort (artist, title, album) from the folder layout and file
    name when the tags don't carry them. Understands Beetdrop's own layout
    ({Artist}/{Album} ({Year})/{NN} - {Title}.ext and the _review folder)
    and a plain "Artist - Title.ext"."""
    stem = path.stem
    title = _TRACK_PREFIX.sub("", stem).strip() or stem
    try:
        dirs = [d for d in path.relative_to(root).parts[:-1] if d != "_review"]
    except ValueError:
        dirs = [d for d in path.parts[:-1] if d != "_review"]

    artist = ""
    album = ""
    if len(dirs) >= 2:
        # {Artist}/{Album} ({Year})/track
        artist = dirs[-2]
        album = _YEAR_SUFFIX.sub("", dirs[-1]).strip()
    elif len(dirs) == 1:
        folder = dirs[-1]
        # A "_review" bucket names its subfolder "Artist - Title".
        if " - " in folder and " - " not in title:
            artist = folder.split(" - ", 1)[0].strip()
        else:
            artist = folder
    # A "Artist - Title" file name fills in a still-missing artist.
    if not artist and " - " in title:
        left, right = title.split(" - ", 1)
        artist, title = left.strip(), right.strip()
    return artist, title, album


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
        # Duration comes from the decoded audio, so it is available even
        # when the text tags are not.
        artist, title, album, duration = meta or ("", "", "", None)
        if not artist or not title:
            # Fall back to the folder/file names for a missing artist/title;
            # keep the real duration read from the file.
            p_artist, p_title, p_album = meta_from_path(path, config.music_root)
            artist = artist or p_artist
            title = title or p_title
            album = album or p_album
        if not (artist and title):
            skipped += 1
        else:
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
