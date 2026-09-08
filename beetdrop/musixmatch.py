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

import re
from typing import Optional

import requests

APP_ID = "web-desktop-app-v1.0"
TOKEN_URL = "https://apic-desktop.musixmatch.com/ws/1.1/token.get"
SUBTITLES_URL = "https://apic-desktop.musixmatch.com/ws/1.1/macro.subtitles.get"
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
        if not response.ok:
            return None
        data = response.json()
    except (requests.RequestException, ValueError):
        return None
    body = _find_subtitle_body(data)
    if body and _LRC_TIMESTAMP.search(body):
        return body.strip()
    return None  # plain-only or empty -> skipped (timed only)


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
