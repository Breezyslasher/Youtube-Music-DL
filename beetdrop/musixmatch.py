"""Musixmatch synced lyrics via the public apic-desktop endpoints.

Musixmatch has the best synced-lyrics coverage, but no free official
lyrics API. The desktop app talks to apic-desktop with a rotating
"usertoken"; token.get hands one out. This module fetches a token (the
in-app "pull latest token" button) and queries macro.subtitles.get for
an LRC subtitle. Timed only: a result without [mm:ss] timestamps is
rejected.

This is best effort and unofficial - tokens can rate-limit or captcha,
so token fetch failures are surfaced to the user, who can also paste a
token from the Musixmatch desktop app instead.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import requests


class MusixmatchUnavailable(RuntimeError):
    """Musixmatch could not be reached; a later run may still succeed.

    Distinct from returning None, which means Musixmatch answered and has
    no synced lyrics for this track.
    """


# "Ask again later" rather than "this track has no lyrics".
RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)

APP_ID = "web-desktop-app-v1.0"
TOKEN_URL = "https://apic-desktop.musixmatch.com/ws/1.1/token.get"
SUBTITLES_URL = "https://apic-desktop.musixmatch.com/ws/1.1/macro.subtitles.get"
# Word-by-word. macro.subtitles.get returns line-level LRC whatever the
# namespace says; the per-word timing lives behind its own endpoint and
# needs the track id the macro call already hands back.
RICHSYNC_URL = "https://apic-desktop.musixmatch.com/ws/1.1/track.richsync.get"
# Musixmatch's own status from the last richsync call, for diagnostics.
# 401 there means the account cannot have word-by-word at all, which is a
# different answer from the track not having any.
LAST_RICHSYNC_STATUS = None
TIMEOUT = 12
# A desktop-app-ish UA; apic-desktop rejects obviously-scripted clients.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_LRC_TIMESTAMP = re.compile(r"\[\d{1,2}:\d{2}")
# f_subtitle_length is a hard filter: on its own it demands the subtitle
# run almost exactly as long as the file, which rejects most tracks. It
# is only usable paired with a deviation allowance.
SUBTITLE_LENGTH_DEVIATION = 15


class MusixmatchError(RuntimeError):
    pass


def fetch_token() -> str:
    """Obtain a fresh usertoken. Raises MusixmatchError on failure so the
    UI can tell the user (and offer the manual-paste path)."""
    try:
        response = requests.get(
            TOKEN_URL,
            params={"app_id": APP_ID, "format": "json"},
            headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise MusixmatchError("could not reach Musixmatch: %s" % exc) from exc
    try:
        body = response.json()["message"]
        status = body["header"]["status_code"]
        token = body["body"]["user_token"]
    except (ValueError, KeyError, TypeError):
        raise MusixmatchError("unexpected token response") from None
    if status != 200 or not token or token == "UpgradeOnlyUpgradeOnlyUpgradeOnlyUpgradeOnly":
        raise MusixmatchError(
            "Musixmatch refused a token (status %s) - it may be rate "
            "limiting; try again shortly or paste a token from the "
            "desktop app" % status)
    return token


def _find_subtitle_body(obj) -> Optional[str]:
    """Dig the LRC subtitle out of macro.subtitles.get's nested JSON,
    resilient to their structure shifting."""
    if isinstance(obj, dict):
        body = obj.get("subtitle_body")
        if isinstance(body, str) and body.strip():
            return body
        for value in obj.values():
            found = _find_subtitle_body(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_subtitle_body(value)
            if found:
                return found
    return None


def _query(params: dict) -> Optional[str]:
    try:
        response = requests.get(SUBTITLES_URL, params=params,
                                headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise MusixmatchUnavailable("could not reach Musixmatch: %s" % exc) from exc
    if response.status_code in RETRYABLE_STATUS:
        raise MusixmatchUnavailable("Musixmatch returned %s" % response.status_code)
    try:
        if not response.ok:
            return None
        data = response.json()
    except ValueError:
        return None
    body = _find_subtitle_body(data)
    if body and _LRC_TIMESTAMP.search(body):
        return body.strip()
    return None  # plain-only or empty -> skipped (timed only)


def inner_status(data) -> Optional[int]:
    """Musixmatch's own status, which is not the HTTP one.

    Every response is wrapped in message.header.status_code, and the
    outer request is 200 whatever it says. 401 (not entitled), 402 and
    404 all arrived here as a plain empty result, indistinguishable from
    "this track has no word timing" - so a subscription wall looked
    exactly like a catalogue gap.
    """
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    if isinstance(message, dict):
        header = message.get("header")
        if isinstance(header, dict):
            code = header.get("status_code")
            if isinstance(code, (int, float)):
                return int(code)
    return None


def _all_tracks(obj, found=None) -> list:
    """Every candidate track object in a response, not just the first.

    A macro response nests several calls, and taking whichever dict
    happened to come first is how the probe reported that 100% of a
    library had word-by-word lyrics.
    """
    if found is None:
        found = []
    if isinstance(obj, dict):
        if "track_id" in obj and "has_richsync" in obj:
            found.append(obj)
        for value in obj.values():
            _all_tracks(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _all_tracks(value, found)
    return found


def _find_track(obj) -> Optional[dict]:
    """The matched track object out of macro.subtitles.get's nested JSON.

    Same defensive walk as _find_subtitle_body, for the same reason: the
    macro response wraps several calls and its shape moves around.
    """
    if isinstance(obj, dict):
        if "track_id" in obj and "has_richsync" in obj:
            return obj
        for value in obj.values():
            found = _find_track(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_track(value)
            if found:
                return found
    return None


def _stamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return "%02d:%05.2f" % (minutes, seconds - minutes * 60)


def richsync_to_lrc(raw: str) -> Optional[str]:
    """Musixmatch richsync JSON to Enhanced (A2) LRC.

    The body is a list of lines, each with a start "ts" and a list "l" of
    fragments carrying the text "c" and an offset "o" measured from that
    line's start. Fragments already include their own spacing, so they
    are concatenated rather than joined - the same rule the Apple side
    had to learn when its syllables came back spaced apart.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(rows, list):
        return None

    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        fragments = row.get("l")
        if not isinstance(fragments, list) or not fragments:
            # A line with no per-word breakdown is still a real line.
            text = " ".join(str(row.get("x") or "").split())
            if text:
                lines.append((start, "[%s]%s" % (_stamp(start), text)))
            continue

        pieces, running, gap = [], start, False
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            body = fragment.get("c")
            if not isinstance(body, str):
                continue
            word = body.strip()
            gap = gap or body[:1].isspace()
            if not word:
                gap = gap or bool(body)
                continue
            try:
                at = start + float(fragment.get("o") or 0)
            except (TypeError, ValueError):
                at = running
            # Time may not run backwards mid-line: a player renders that
            # as a jump. Same rule as the Apple renderer.
            if at < running:
                at = running
            running = at
            if pieces and gap:
                pieces.append(" ")
            pieces.append("<%s>%s" % (_stamp(at), word))
            gap = body[-1:].isspace()
        if pieces:
            lines.append((start, "[%s]%s" % (_stamp(start), "".join(pieces))))

    if not lines:
        return None
    lines.sort(key=lambda pair: pair[0])
    return "\n".join(line for _, line in lines)


def probe_track(token: str, artist: str, title: str,
                duration_seconds: Optional[int] = None) -> Optional[dict]:
    """What Musixmatch matched this track to, and whether it claims to
    have word-by-word lyrics for it.

    One request - the same macro call the line-level fetch already makes,
    read for the track object rather than the subtitle.
    """
    if not token or not artist or not title:
        return None
    params = {
        "format": "json",
        "namespace": "lyrics_richsynched",
        "subtitle_format": "lrc",
        "app_id": APP_ID,
        "usertoken": token,
        "q_track": title,
        "q_artist": artist,
    }
    if duration_seconds:
        params["q_duration"] = str(int(duration_seconds))
    try:
        response = requests.get(SUBTITLES_URL, params=params,
                                headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise MusixmatchUnavailable("could not reach Musixmatch: %s" % exc) from exc
    if response.status_code in RETRYABLE_STATUS:
        raise MusixmatchUnavailable("Musixmatch returned %s" % response.status_code)
    try:
        if not response.ok:
            return None
        track = _find_track(response.json())
    except ValueError:
        return None
    if not track:
        return None
    found = {
        "track_id": track.get("track_id"),
        "has_richsync": bool(track.get("has_richsync")),
        "artist": track.get("artist_name") or "",
        "title": track.get("track_name") or "",
        "length": track.get("track_length"),
    }
    found["looks_right"] = looks_like_the_track(artist, title, found)
    return found


def looks_like_the_track(artist: str, title: str, found: dict) -> bool:
    """Is this actually the track we asked about?

    Musixmatch answers with its best effort rather than nothing, so
    "matched" on its own means only that a request succeeded - the first
    probe reported a 100% match rate on a library with rough tags, which
    is the tell. The Apple path has required this all along; the
    Musixmatch path never did.
    """
    from .matching import base_title, normalize, normalize_artist, _ratio
    from .matching import significant_qualifiers

    theirs_title = found.get("title") or ""
    theirs_artist = found.get("artist") or ""
    # An absent name is not a disagreement - only a real one rejects.
    if theirs_title and title:
        if _ratio(normalize(base_title(title)),
                  normalize(base_title(theirs_title))) < 0.85:
            return False
        if significant_qualifiers(title) != significant_qualifiers(theirs_title):
            return False
    if theirs_artist and artist:
        ours, mine = normalize_artist(artist), normalize_artist(theirs_artist)
        if _ratio(ours, mine) < 0.75 and not (ours.startswith(mine)
                                              or mine.startswith(ours)):
            return False
    return True


def fetch_richsync(token: str, track_id, duration_seconds: Optional[int] = None
                   ) -> Optional[str]:
    """Enhanced LRC for one Musixmatch track id, or None when there is no
    word-by-word version. Never raises for a plain miss."""
    if not token or not track_id:
        return None
    params = {
        "format": "json",
        "app_id": APP_ID,
        "usertoken": token,
        "track_id": str(track_id),
    }
    if duration_seconds:
        params["f_subtitle_length"] = str(int(duration_seconds))
    try:
        response = requests.get(RICHSYNC_URL, params=params,
                                headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise MusixmatchUnavailable("could not reach Musixmatch: %s" % exc) from exc
    if response.status_code in RETRYABLE_STATUS:
        raise MusixmatchUnavailable("Musixmatch returned %s" % response.status_code)
    try:
        if not response.ok:
            return None
        data = response.json()
    except ValueError:
        return None
    global LAST_RICHSYNC_STATUS
    LAST_RICHSYNC_STATUS = inner_status(data)
    body = _find_richsync_body(data)
    return richsync_to_lrc(body) if body else None


def _find_richsync_body(obj) -> Optional[str]:
    if isinstance(obj, dict):
        body = obj.get("richsync_body")
        if isinstance(body, str) and body.strip():
            return body
        for value in obj.values():
            found = _find_richsync_body(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_richsync_body(value)
            if found:
                return found
    return None


def fetch_synced(token: str, artist: str, title: str,
                 duration_seconds: Optional[int] = None) -> Optional[str]:
    """The LRC text for this track from Musixmatch, or None when there
    are no *synced* lyrics. Never raises - a miss is a normal outcome.

    Duration is used to pick the right recording, but only as a hint:
    the length filter is tried with a deviation allowance first, then
    dropped entirely, because an over-tight filter turns a track that
    Musixmatch does have into a miss.
    """
    if not token or not artist or not title:
        return None
    base = {
        "format": "json",
        "namespace": "lyrics_richsynched",
        "subtitle_format": "lrc",
        "app_id": APP_ID,
        "usertoken": token,
        "q_track": title,
        "q_artist": artist,
    }

    attempts = []
    if duration_seconds:
        seconds = str(int(duration_seconds))
        attempts.append(dict(base, q_duration=seconds,
                             f_subtitle_length=seconds,
                             f_subtitle_length_max_deviation=str(
                                 SUBTITLE_LENGTH_DEVIATION)))
        attempts.append(dict(base, q_duration=seconds))
    attempts.append(dict(base))

    for params in attempts:
        lrc = _query(params)
        if lrc:
            return lrc
    return None
