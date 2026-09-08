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
from typing import Optional

import requests

from . import musixmatch
from .mb import USER_AGENT

LRCLIB_GET = "https://lrclib.net/api/get"
TIMEOUT = 10

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


def _lrclib(artist: str, title: str, album: str,
            duration_seconds: Optional[int]) -> Optional[str]:
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_seconds:
        params["duration"] = str(int(duration_seconds))
    try:
        response = requests.get(LRCLIB_GET, params=params, timeout=TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
    except requests.RequestException:
        return None
    if not response.ok:  # 404 = no lyrics known; normal
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    synced = (data.get("syncedLyrics") or "").strip()
    return synced or None  # plain-only results are intentionally skipped


def _musixmatch(artist, title, album, duration_seconds, token):
    if not token:
        return None
    try:
        return musixmatch.fetch_synced(token, artist, title, duration_seconds)
    except Exception:
        return None


def fetch_synced_lyrics(artist: str, title: str, album: str = "",
                        duration_seconds: Optional[int] = None,
                        musixmatch_token: str = "",
                        provider: str = "lrclib") -> Optional[str]:
    """The LRC text for this track, or None when no *synced* lyrics exist.

    `provider` picks which source is tried first ("lrclib" or
    "musixmatch"); the other is the fallback. Musixmatch is only
    attempted when a token is configured. Duration is passed whenever
    known so both sources return the correctly-timed version.
    """
    if not artist or not title:
        return None
    lrclib = lambda: _lrclib(artist, title, album, duration_seconds)
    mxm = lambda: _musixmatch(artist, title, album, duration_seconds, musixmatch_token)
    order = [mxm, lrclib] if provider == "musixmatch" else [lrclib, mxm]
    for source in order:
        lrc = source()
        # Never accept generated placeholder text, whatever returned it.
        if lrc and not looks_synthetic(lrc):
            return lrc
    return None
