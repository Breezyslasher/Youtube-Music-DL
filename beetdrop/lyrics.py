"""Synced (timed) lyrics.

Timed only: only LRC text with [mm:ss.xx] timestamps is used; plain
lyrics are always skipped. Primary source is LRCLIB (free, no token);
Musixmatch is an optional fallback when a usertoken is configured and
LRCLIB has nothing. Best effort - no lyrics is normal, never an error -
and written as a .lrc sidecar next to the audio file, the format Plex,
Navidrome, and most players read for synced lyrics.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Optional

import requests

from . import apple, musixmatch
from .matching import base_title, normalize_artist
from .mb import USER_AGENT

LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
TIMEOUT = 10
# How far a fuzzy search hit may be from the file's real duration.
SEARCH_DURATION_TOLERANCE = 8
# /api/search is a loose text match and will happily return a different
# song, so a candidate has to actually look like what we asked for before
# its lyrics are accepted. Wrong lyrics are worse than none.
SEARCH_MIN_TITLE_RATIO = 0.85
SEARCH_MIN_ARTIST_RATIO = 0.75

# "(feat. X)", "(Remastered 2011)", "(Live)" - a trailing parenthetical
# that the lyrics database usually does not carry in its track name.
_PAREN_SUFFIX = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")
_FEAT = re.compile(r"\s*\b(?:feat|ft|featuring)\b\.?\s+.*$", re.I)
# Conservative: "&" and "/" are left alone so Hall & Oates and AC/DC survive.
_ARTIST_SEP = re.compile(r"\s*(?:,|;|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*", re.I)

_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
# A placeholder generator spaces every line identically; real transcribed
# lyrics never do. Require a good number of lines before judging.
_MIN_LINES_TO_JUDGE = 8
_UNIFORM_RATIO = 0.95


def lrc_times(lrc: str) -> list:
    """Every [mm:ss.xx] timestamp in an LRC, as seconds."""
    times = []
    for match in _TIMESTAMP.finditer(lrc or ""):
        minutes, seconds, frac = match.groups()
        value = int(minutes) * 60 + int(seconds)
        if frac:
            value += int(frac) / (100.0 if len(frac) <= 2 else 1000.0)
        times.append(value)
    return times


def looks_synthetic(lrc: str) -> bool:
    """True when an LRC's lines are perfectly evenly spaced - the
    signature of generated placeholder text, not of real lyrics.

    Deliberately language-agnostic: it judges only the timing, so real
    lyrics in any language (including romaji transliterations that can
    look like nonsense words) are never rejected.
    """
    times = lrc_times(lrc)
    if len(times) < _MIN_LINES_TO_JUDGE:
        return False
    gaps = [round(b - a, 2) for a, b in zip(times, times[1:]) if b >= a]
    if len(gaps) < _MIN_LINES_TO_JUDGE - 1:
        return False
    gap, hits = Counter(gaps).most_common(1)[0]
    return gap > 0 and hits / len(gaps) >= _UNIFORM_RATIO


def has_word_timing(lrc: str) -> bool:
    """True for Enhanced (A2) LRC - inline <mm:ss.xx> tags before words."""
    return re.search(r"<\d{1,2}:\d{2}[.:]\d{1,3}>", lrc or "") is not None


_WORD_TAG = re.compile(r"<(\d{1,2}):(\d{2}[.:]\d{1,3})>")


def has_backwards_word_timing(lrc: str) -> bool:
    """True when some line's word tags run backwards.

    A renderer highlighting word by word would jump back mid-line. Real
    lyrics never do this; it only comes from a bad conversion, so a file
    like it is worth fetching again.
    """
    for line in (lrc or "").splitlines():
        times = [int(m[0]) * 60 + float(m[1].replace(":", "."))
                 for m in _WORD_TAG.findall(line)]
        if any(later < earlier for earlier, later in zip(times, times[1:])):
            return True
    return False


def _primary_artist(artist: str) -> str:
    """"Billie Eilish, Khalid" -> "Billie Eilish"; the name a lyrics
    database files the track under."""
    return _ARTIST_SEP.split(artist, 1)[0].strip() or artist.strip()


def _simplify_title(title: str) -> str:
    """"lovely (with Khalid)" / "Song (Remastered)" -> the bare title."""
    simple = _PAREN_SUFFIX.sub("", _FEAT.sub("", title)).strip()
    return simple or title.strip()


def _get_json(url: str, params: dict):
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
    except requests.RequestException:
        return None
    if not response.ok:  # 404 = no lyrics known; normal
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _synced_of(row) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    # Plain-only results are intentionally skipped.
    return (row.get("syncedLyrics") or "").strip() or None


def _lrclib_get(artist: str, title: str, album: str,
                duration_seconds: Optional[int]) -> Optional[str]:
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_seconds:
        params["duration"] = str(int(duration_seconds))
    return _synced_of(_get_json(LRCLIB_GET, params))


def _is_same_track(row: dict, artist: str, title: str) -> bool:
    """Whether a search hit really is the track we asked for. /api/search
    matches loosely, so without this a generic title can pull back a
    completely different song's lyrics."""
    def ratio(a, b):
        return SequenceMatcher(None, a, b).ratio()

    if ratio(base_title(row.get("trackName") or ""),
             base_title(title)) < SEARCH_MIN_TITLE_RATIO:
        return False
    return ratio(normalize_artist(row.get("artistName") or ""),
                 normalize_artist(artist)) >= SEARCH_MIN_ARTIST_RATIO


def _lrclib_search(artist: str, title: str,
                   duration_seconds: Optional[int]) -> Optional[str]:
    """Fuzzy lookup: of the candidates that really are this track, take
    the synced one closest to our duration."""
    rows = _get_json(LRCLIB_SEARCH,
                     {"artist_name": artist, "track_name": title})
    if not isinstance(rows, list):
        return None
    best, best_delta = None, None
    for row in rows:
        synced = _synced_of(row)
        if not synced or not _is_same_track(row, artist, title):
            continue
        row_duration = row.get("duration")
        if duration_seconds and row_duration:
            delta = abs(float(row_duration) - float(duration_seconds))
            if delta > SEARCH_DURATION_TOLERANCE:
                continue  # a different recording of the same song
        else:
            delta = float("inf")
        if best is None or delta < best_delta:
            best, best_delta = synced, delta
    return best


def _lrclib(artist: str, title: str, album: str,
            duration_seconds: Optional[int]) -> Optional[str]:
    """LRCLIB, tried from most precise to most forgiving.

    /api/get is an exact match - the album has to agree and the duration
    must be within ~2s - so one strict call misses a lot in practice:
    album names differ between taggers, and a YouTube rip is often a
    couple of seconds off the release. Each step drops one constraint,
    and /api/search is the fuzzy last resort.
    """
    artist, title = artist.strip(), title.strip()
    primary, simple = _primary_artist(artist), _simplify_title(title)
    loosened = (primary, simple) != (artist, title)

    attempts = [(artist, title, album, duration_seconds),
                (artist, title, "", duration_seconds)]
    if loosened:
        attempts.append((primary, simple, "", duration_seconds))

    seen = set()
    for one_artist, one_title, one_album, one_duration in attempts:
        key = (one_artist.lower(), one_title.lower(), one_album.lower(), one_duration)
        if key in seen:
            continue
        seen.add(key)
        lrc = _lrclib_get(one_artist, one_title, one_album, one_duration)
        if lrc:
            return lrc

    for one_artist, one_title in ([(artist, title), (primary, simple)]
                                  if loosened else [(artist, title)]):
        lrc = _lrclib_search(one_artist, one_title, duration_seconds)
        if lrc:
            return lrc
    return None


def _musixmatch(artist, title, album, duration_seconds, token):
    if not token:
        return None
    try:
        return musixmatch.fetch_synced(token, artist, title, duration_seconds)
    except Exception:
        return None


def _apple(artist, title, duration_seconds, token, storefront, word_by_word):
    if not token:
        return None
    try:
        return apple.fetch_synced(token, artist, title, duration_seconds,
                                  storefront=storefront or "us",
                                  word_by_word=word_by_word)
    except Exception:
        return None


PROVIDERS = ("lrclib", "musixmatch", "apple")


def fetch_synced_lyrics(artist: str, title: str, album: str = "",
                        duration_seconds: Optional[int] = None,
                        musixmatch_token: str = "",
                        provider: str = "lrclib",
                        apple_token: str = "",
                        apple_storefront: str = "us",
                        word_by_word: bool = False) -> Optional[str]:
    """The LRC text for this track, or None when no *synced* lyrics exist.

    `provider` picks which source is tried first; the others follow as
    fallbacks. Musixmatch and Apple are only attempted when their token is
    configured, so an unconfigured source costs nothing. Duration is
    passed whenever known so every source returns the right recording.

    Apple is the only source that can supply per-word timing; with
    word_by_word it emits Enhanced (A2) LRC when Apple has it.
    """
    if not artist or not title:
        return None

    memo = {}

    def source(name):
        """Each source is asked at most once per track."""
        if name not in memo:
            if name == "lrclib":
                memo[name] = _lrclib(artist, title, album, duration_seconds)
            elif name == "musixmatch":
                memo[name] = _musixmatch(artist, title, album,
                                         duration_seconds, musixmatch_token)
            else:
                memo[name] = _apple(artist, title, duration_seconds,
                                    apple_token, apple_storefront, word_by_word)
        return memo[name]

    def usable(lrc):
        # Never accept generated placeholder text, whatever returned it.
        return bool(lrc) and not looks_synthetic(lrc)

    # Apple is the only source with per-word timing, so when that is what
    # was asked for it has to be tried first - otherwise a line-level hit
    # from a source earlier in the order wins and Apple is never asked,
    # silently downgrading every track the others happen to have.
    if word_by_word and apple_token:
        lrc = source("apple")
        if usable(lrc) and has_word_timing(lrc):
            return lrc

    primary = provider if provider in PROVIDERS else "lrclib"
    order = [primary] + [name for name in PROVIDERS if name != primary]
    for name in order:
        lrc = source(name)
        if usable(lrc):
            return lrc
    return None
