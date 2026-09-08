"""Backfill synced lyrics for tracks already in the library.

Walks the music library, finds audio files that have no .lrc sidecar yet,
reads their tags, and fetches synced lyrics through the same
LRCLIB/Musixmatch pipeline a fresh grab uses. Best effort per track: a
missing match or a network error is counted and skipped, never fatal.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import mutagen

from .cleaning import clean_title
from .config import Config
from .library import write_lyrics_sidecar
from .lyrics import (
    LyricsUnavailable,
    fetch_synced_lyrics,
    has_backwards_word_timing,
    has_word_timing,
    looks_synthetic,
)
from .matching import normalize_artist

# Leading track (and disc) number on a filename: "02 - ", "1-02 - ", "02. ",
# and - the common case this missed for a long time - a bare "06 Title" with
# nothing but a space after the number. Requiring punctuation meant every
# "NN Title.m4a" in a ripped album was searched for as "06 Chance To Love
# You More", which no catalogue has. "2-04 Title" was worse: it stripped
# only the disc part and searched for "04 Title".
_TRACK_PREFIX = re.compile(r"^(?:\d{1,2}\s*-\s*)?\d{1,3}(?:\s*[-.]\s*|\s+)(?=\S)")
# Runs of whitespace, which junk tags are full of.
_WHITESPACE = re.compile(r"\s+")
# Trailing " (1999)" year on an album folder.
_YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")

# Audio we tag/file; the video library's .mp4 files are ignored.
AUDIO_EXTS = (".opus", ".ogg", ".mp3", ".m4a", ".flac")
# Seconds to wait between lookups. This was dropped to 0 to speed up a
# full-library pass and Apple started returning 429 on a real library, so
# it is back: running flat out is what provoked the rate limit, and once
# provoked it blocks the Settings token test too, not just the scan.
# 0.2 is the value that ran for weeks without complaint. Set
# LYRICS_REQUEST_SPACING=0 to run flat out anyway.
REQUEST_SPACING = float(os.environ.get("LYRICS_REQUEST_SPACING", "0.2"))


def _noop(*args) -> None:
    pass


@dataclass
class CoverageEstimate:
    """How much of the library Apple could serve word-by-word, from a
    random sample rather than a full pass.

    Checking every line-level track costs two Apple calls each and hours
    of rate-limited waiting, only to answer "was that worth running?".
    A sample of a hundred answers it in a couple of minutes, and the
    margin says how much to trust the number.
    """
    population: int = 0    # line-level tracks an upgrade would visit
    sampled: int = 0       # of those, how many Apple actually answered for
    word_level: int = 0    # of the answers, how many had word timing
    deferred: int = 0      # asked but rate-limited or unreachable
    no_match: int = 0      # Apple had nothing for the track at all

    @property
    def pct(self) -> float:
        return 100.0 * self.word_level / self.sampled if self.sampled else 0.0

    @property
    def margin(self) -> float:
        """95% confidence half-width in percentage points.

        Includes the finite-population correction: a hundred tracks out
        of three thousand is a real slice of the whole, so the interval
        is tighter than the textbook infinite-population one.
        """
        if self.sampled < 2:
            return 100.0
        share = self.word_level / self.sampled
        spread = (share * (1 - share) / self.sampled) ** 0.5
        if self.population > self.sampled:
            spread *= ((self.population - self.sampled)
                       / (self.population - 1)) ** 0.5
        return 100.0 * 1.96 * spread

    @property
    def projected(self) -> int:
        """Tracks in the whole population the estimate implies."""
        return int(round(self.population * self.pct / 100.0))


def estimate_word_coverage(config: Config, sample: int = 100,
                           on_detail: Callable[[str], None] = _noop,
                           on_progress: Callable[[float], None] = _noop,
                           seed: Optional[int] = None) -> CoverageEstimate:
    """Ask Apple about a random sample of the tracks an upgrade would
    visit, and report what share of them Apple has word timing for.

    Read-only: nothing is written, so this is safe to run against a
    library at any time, and safe to interrupt.
    """
    import random

    from .lyrics import LyricsUnavailable, fetch_synced_lyrics

    files = list(iter_audio_line_level_lyrics(config.music_root))
    result = CoverageEstimate(population=len(files))
    if not files:
        return result
    chosen = (files if sample <= 0 or sample >= len(files)
              else random.Random(seed).sample(files, sample))
    on_detail("asking Apple about %d of %d tracks..." % (len(chosen), len(files)))

    for index, path in enumerate(chosen):
        artist, title, album, duration = read_track_meta(path) or ("", "", "", None)
        if not artist or not title:
            p_artist, p_title, p_album = meta_from_path(path, config.music_root)
            artist, title, album = artist or p_artist, title or p_title, album or p_album
        title = tidy_track_name(title, artist)
        if not (artist and title):
            result.no_match += 1
        else:
            try:
                lrc = fetch_synced_lyrics(
                    artist, title, album, duration,
                    musixmatch_token=config.musixmatch_token,
                    provider=config.lyrics_provider,
                    apple_token=config.apple_token,
                    apple_storefront=config.apple_storefront,
                    word_by_word=True, word_only=True)
            except LyricsUnavailable:
                result.deferred += 1
                lrc = None
            except Exception:
                lrc = None
            else:
                result.sampled += 1
                if lrc and has_word_timing(lrc):
                    result.word_level += 1
                else:
                    result.no_match += 1
        if REQUEST_SPACING:
            time.sleep(REQUEST_SPACING)
        on_progress((index + 1) / len(chosen) * 100.0)
        on_detail("%d/%d checked, %d have word-by-word available"
                  % (index + 1, len(chosen), result.word_level))
    return result


@dataclass
class BackfillResult:
    total: int      # audio files found without a .lrc sidecar
    added: int      # sidecars written
    skipped: int    # files with no usable artist/title tags
    no_match: int   # looked up but no synced lyrics found
    upgraded: int = 0  # line-level sidecars replaced with word-level
    purged: int = 0  # bogus placeholder sidecars deleted first
    # Tracks a source could not be asked about - rate limits, a rotated
    # token, a dropped connection. Deliberately not counted as no_match:
    # these may well have lyrics, and running the pass again picks them
    # up, so a run that hits many is incomplete rather than finished.
    deferred: int = 0


@dataclass
class LyricsStats:
    """A read-only picture of the library's lyric coverage."""
    audio_files: int = 0
    with_lyrics: int = 0
    word_level: int = 0    # Enhanced (A2) - inline per-word timing
    line_level: int = 0
    missing: int = 0
    placeholder: int = 0   # generated junk still sitting in the library
    # Word-level files whose tags run backwards mid-line: they look like
    # a win in the word_level count but a player highlighting them jumps
    # about, so they are called out separately. Counted inside word_level.
    backwards: int = 0
    # Which tracks fall in each bucket, as paths relative to the library.
    # Capped by the caller so a big library cannot flood a response.
    line_level_files: list = field(default_factory=list)
    missing_files: list = field(default_factory=list)
    placeholder_files: list = field(default_factory=list)
    backwards_files: list = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.with_lyrics / self.audio_files if self.audio_files else 0.0

    @property
    def word_pct(self) -> float:
        return 100.0 * self.word_level / self.with_lyrics if self.with_lyrics else 0.0


def lyrics_stats(root: Path, sample: int = 50) -> LyricsStats:
    """Count how much of the library has lyrics, and how much of that is
    word-by-word, naming the tracks in each bucket.

    sample caps how many names are collected per bucket; 0 collects every
    one, which is what the CLI uses so the output can be piped. Reads
    only; nothing is written or fetched.
    """
    stats = LyricsStats()

    def note(bucket: list, path: Path) -> None:
        if sample == 0 or len(bucket) < sample:
            try:
                bucket.append(str(path.relative_to(root)))
            except ValueError:
                bucket.append(str(path))

    for path in sorted(root.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTS):
            continue
        stats.audio_files += 1
        sidecar = path.with_suffix(".lrc")
        if not sidecar.is_file():
            note(stats.missing_files, path)
            continue
        stats.with_lyrics += 1
        try:
            text = sidecar.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if has_word_timing(text):
            stats.word_level += 1
            if has_backwards_word_timing(text):
                stats.backwards += 1
                note(stats.backwards_files, path)
        else:
            stats.line_level += 1
            note(stats.line_level_files, path)
        if looks_synthetic(text):
            stats.placeholder += 1
            note(stats.placeholder_files, path)
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
        # spacing and case differences do not stop the strip. The dash
        # needs no spaces around it: "Blue October-Conversation Via Radio"
        # is how a lot of ripped files are named.
        head = re.match(r"^(.*?)\s*[-–—:]\s*(.+)$", cleaned)
        if head and normalize_artist(head.group(1)) == normalize_artist(artist):
            cleaned = head.group(2)
        else:
            # No separator at all - "Adele I Found A Boy", "Blue October
            # The Still". Peel words off the front while they still spell
            # the artist, and never take the whole title.
            wanted = normalize_artist(artist)
            words = cleaned.split()
            for count in range(len(words) - 1, 0, -1):
                if normalize_artist(" ".join(words[:count])) == wanted:
                    cleaned = " ".join(words[count:])
                    break
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
    added = skipped = no_match = upgraded = deferred = 0

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
                    # timing, so ask for it whatever the standing setting -
                    # and accept nothing else, which keeps the pass to the
                    # two Apple calls instead of the full ten-request chain.
                    word_by_word=True if upgrade else config.word_lyrics,
                    word_only=upgrade)
                unavailable = None
            except LyricsUnavailable as exc:
                # Not a miss: nobody could answer. Leave the track alone so
                # a later run retries it, and say so rather than recording
                # a "no lyrics found" that is not true.
                lrc, unavailable = None, exc
            except Exception:
                lrc, unavailable = None, None
            if unavailable is not None:
                deferred += 1
                if deferred <= 5:
                    on_detail("deferred %s - %s" % (path.name, unavailable))
                elif deferred == 6:
                    on_detail("deferred: further errors not listed individually")
            elif upgrade:
                # Only replace when the answer is actually better. Word
                # timing that runs backwards is not: overwriting a sound
                # line-level sidecar with it makes the track worse, and
                # counting it as an upgrade would report a repair that did
                # not happen.
                if lrc and has_word_timing(lrc) and not has_backwards_word_timing(lrc):
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
            if REQUEST_SPACING:
                time.sleep(REQUEST_SPACING)
        on_detail("%d/%d checked, %d %s" % (
            index + 1, total, upgraded if upgrade else added,
            "upgraded to word-by-word" if upgrade else "lyrics added"))
        if total:
            on_progress((index + 1) / total * 100.0)

    return BackfillResult(total=total, added=added, skipped=skipped,
                          no_match=no_match, upgraded=upgraded, purged=purged,
                          deferred=deferred)
