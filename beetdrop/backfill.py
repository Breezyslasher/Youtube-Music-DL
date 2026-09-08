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

from .cleaning import clean_title
from .config import Config
from .library import write_lyrics_sidecar
from .lyrics import (
    fetch_synced_lyrics,
    has_backwards_word_timing,
    has_word_timing,
    looks_synthetic,
)
from .matching import normalize_artist

# Leading track (and disc) number on a filename, e.g. "02 - ", "1-02 - ".
_TRACK_PREFIX = re.compile(r"^(?:\d+-)?\d+\s*[-.]\s*")
# Runs of whitespace, which junk tags are full of.
_WHITESPACE = re.compile(r"\s+")
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
    upgraded: int = 0  # line-level sidecars replaced with word-level
    purged: int = 0  # bogus placeholder sidecars deleted first


@dataclass
class LyricsStats:
    """A read-only picture of the library's lyric coverage."""
    audio_files: int = 0
    with_lyrics: int = 0
    word_level: int = 0    # Enhanced (A2) - inline per-word timing
    line_level: int = 0
    missing: int = 0
    placeholder: int = 0   # generated junk still sitting in the library

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.with_lyrics / self.audio_files if self.audio_files else 0.0

    @property
    def word_pct(self) -> float:
        return 100.0 * self.word_level / self.with_lyrics if self.with_lyrics else 0.0


def lyrics_stats(root: Path) -> LyricsStats:
    """Count how much of the library has lyrics, and how much of that is
    word-by-word. Reads only; nothing is written or fetched."""
    stats = LyricsStats()
    for path in sorted(root.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTS):
            continue
        stats.audio_files += 1
        sidecar = path.with_suffix(".lrc")
        if not sidecar.is_file():
            continue
        stats.with_lyrics += 1
        try:
            text = sidecar.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if has_word_timing(text):
            stats.word_level += 1
        else:
            stats.line_level += 1
        if looks_synthetic(text):
            stats.placeholder += 1
    stats.missing = stats.audio_files - stats.with_lyrics
    return stats


def iter_lyrics_files(root: Path):
    for path in sorted(root.rglob("*.lrc")):
        if path.is_file():
            yield path


def find_bad_lyrics(root: Path) -> list:
    """Existing .lrc sidecars that are generated placeholder text rather
    than real lyrics (perfectly uniform line timing)."""
    bad = []
    for path in iter_lyrics_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if looks_synthetic(text):
            bad.append(path)
    return bad


def purge_bad_lyrics(root: Path, on_detail: Callable[[str], None] = None) -> int:
    """Delete placeholder .lrc sidecars so they can be re-fetched. Only
    files that fail the synthetic check are removed - real lyrics, in any
    language, are never touched."""
    removed = 0
    for path in find_bad_lyrics(root):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    if on_detail:
        on_detail("removed %d placeholder lyric files" % removed)
    return removed


def iter_audio_missing_lyrics(root: Path):
    """Every audio file under root that has no .lrc sidecar yet, in a
    stable order."""
    for path in sorted(root.rglob("*")):
        if (path.is_file() and path.suffix.lower() in AUDIO_EXTS
                and not path.with_suffix(".lrc").exists()):
            yield path


def iter_audio_line_level_lyrics(root: Path):
    """Audio whose .lrc an upgrade pass could improve.

    Either it carries no per-word timing at all, or its word tags run
    backwards somewhere - which only a bad conversion produces, and which
    a re-fetch repairs. A sound word-level file, or no sidecar at all, is
    left to the other passes.
    """
    for path in sorted(root.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTS):
            continue
        sidecar = path.with_suffix(".lrc")
        if not sidecar.is_file():
            continue
        try:
            text = sidecar.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not has_word_timing(text) or has_backwards_word_timing(text):
            yield path


def tidy_track_name(title: str, artist: str) -> str:
    """Make a usable track name out of whatever a tagger left behind.

    Files from other downloaders often carry no artist tag and the whole
    "Artist - Title (Audio)" string as the title. Searching a catalogue
    with that finds nothing, so the YouTube noise is stripped and a
    leading artist prefix removed - "5 Seconds of Summer - Social
    Casualty (Audio)" becomes "Social Casualty".
    """
    cleaned = clean_title(title or "")
    if artist:
        # "<artist> - <track>" is the usual shape; compare loosely so
        # spacing and case differences do not stop the strip.
        head = re.match(r"^(.*?)\s+-\s+(.+)$", cleaned)
        if head:
            left, right = head.group(1), head.group(2)
            if normalize_artist(left) == normalize_artist(artist):
                cleaned = right
    return _WHITESPACE.sub(" ", cleaned).strip() or (title or "").strip()


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
    purge_bad: bool = False,
    upgrade: bool = False,
) -> BackfillResult:
    """Fetch and write .lrc sidecars for library tracks missing them.

    purge_bad first deletes placeholder sidecars (generated junk), so the
    tracks they were blocking get looked up fresh in the same run.

    upgrade instead revisits tracks that already have a line-level sidecar
    and replaces it when a per-word version can be had. It is the one pass
    that overwrites an existing file, and only ever with the richer form of
    the same lyrics: a result without word timing is discarded rather than
    written over what is already there.

    on_progress/on_detail let the job layer mirror the scan and also act as
    cancellation checkpoints. `files` lets a caller pre-compute the list.
    """
    purged = 0
    if purge_bad:
        on_detail("checking existing lyrics for placeholder junk...")
        purged = purge_bad_lyrics(config.music_root, on_detail)
    if files is None:
        files = list(iter_audio_line_level_lyrics(config.music_root) if upgrade
                     else iter_audio_missing_lyrics(config.music_root))
    total = len(files)
    added = skipped = no_match = upgraded = 0

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
        # Tag titles are not trustworthy on files other tools wrote.
        title = tidy_track_name(title, artist)
        if not (artist and title):
            skipped += 1
        else:
            try:
                lrc = fetch_synced_lyrics(
                    artist, title, album, duration,
                    musixmatch_token=config.musixmatch_token,
                    provider=config.lyrics_provider,
                    apple_token=config.apple_token,
                    apple_storefront=config.apple_storefront,
                    # An upgrade run is an explicit request for per-word
                    # timing, so ask for it whatever the standing setting.
                    word_by_word=True if upgrade else config.word_lyrics)
            except Exception:
                lrc = None
            if upgrade:
                # Only replace when the answer is actually better.
                if lrc and has_word_timing(lrc):
                    try:
                        write_lyrics_sidecar(path, lrc, overwrite=True)
                        upgraded += 1
                    except Exception:
                        no_match += 1
                else:
                    no_match += 1
            elif lrc:
                try:
                    write_lyrics_sidecar(path, lrc)
                    added += 1
                except Exception:
                    no_match += 1  # write failed; treat as not added
            else:
                no_match += 1
            time.sleep(REQUEST_SPACING)
        on_detail("%d/%d checked, %d %s" % (
            index + 1, total, upgraded if upgrade else added,
            "upgraded to word-by-word" if upgrade else "lyrics added"))
        if total:
            on_progress((index + 1) / total * 100.0)

    return BackfillResult(total=total, added=added, skipped=skipped,
                          no_match=no_match, upgraded=upgraded, purged=purged)
