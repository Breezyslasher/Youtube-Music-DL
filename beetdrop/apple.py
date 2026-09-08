"""Apple Music lyrics.

Apple's catalog API serves lyrics as TTML. /syllable-lyrics is the better
endpoint of the two: it returns word-level timing when Apple has it and
falls back to line-level otherwise, so one request always gets the best
available quality.

Two credentials are needed and only one of them is personal:

  - a developer token, which is a public JWT shipped inside the Music web
    player and is fetched (and cached) automatically here;
  - a media-user-token, which identifies a subscribed account and must be
    supplied by the user in Settings.

Lyrics are subscriber-gated licensed content, so this is off by default
and only ever writes sidecars into the user's own library.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional
from xml.etree import ElementTree as ET

import requests

SEARCH_URL = "https://amp-api.music.apple.com/v1/catalog/%s/search"
LYRICS_URL = "https://amp-api.music.apple.com/v1/catalog/%s/songs/%s/syllable-lyrics"
WEB_PLAYER = "https://music.apple.com/us/browse"
TIMEOUT = 15
# How far a catalog hit may be from the file's duration to be the same
# recording. Apple reports milliseconds.
DURATION_TOLERANCE = 8
# Back off and retry when Apple returns 429 rather than reporting a miss.
# There is no fixed pause between lookups, so this is what keeps a
# full-library scan from running the catalog API too hard.
THROTTLE_RETRIES = 4
THROTTLE_BACKOFF = 1.0   # seconds, doubled each retry
THROTTLE_MAX_WAIT = 30.0   # longest single sleep, so a wait stays interruptible
# amp-api sends 429 with no Retry-After, so this is the wait that actually
# applies in practice. Seconds, not the sub-second retry delay: the limit
# outlasts that by a wide margin.
THROTTLE_BLIND_WAIT = 60.0
# Ceiling on the shared deadline, so a scan pauses rather than hanging.
THROTTLE_MAX_HOLD = 900.0
# Shared deadline: once Apple returns 429, every call waits, not just the
# retries of the one that hit it.
_throttle_until = 0.0
_throttle_lock = threading.Lock()
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")
_ASSET = re.compile(r'src="(/assets/[^"]+\.js)"')

_TTML_NS = "{http://www.w3.org/ns/ttml}"
_ITUNES_TIMING = "{http://music.apple.com/lyric-ttml-internal}timing"
_TTM_ROLE = "{http://www.w3.org/ns/ttml#metadata}role"

# The developer token is public but rotates; cache it rather than scraping
# the web player for every track.
_DEV_TOKEN_TTL = 6 * 3600
_dev_token = {"value": "", "at": 0.0}
_dev_lock = threading.Lock()
# Rescraping the web player is expensive, so a rejected token is replaced
# at most this often however many tracks hit the rejection.
_REFRESH_COOLDOWN = 300.0
_last_refresh = 0.0


class AppleError(RuntimeError):
    pass


class AppleUnavailable(AppleError):
    """Apple could not answer; a later run may still succeed.

    Raised for a dead connection, an exhausted 429 backoff, a token still
    refused after a refresh, and Apple's own 5xx - none of which mean the
    track has no lyrics, which is what returning None would say.
    """


# Statuses that mean "ask again later". A 404 is not one: it is Apple
# saying it has no lyrics for this song, which is a real answer.
UNAVAILABLE_STATUS = (0, 401, 403, 408, 425, 429, 500, 502, 503, 504)


def _headers(developer_token: str, media_user_token: str) -> dict:
    return {
        "Authorization": "Bearer " + developer_token,
        "Media-User-Token": media_user_token,
        "Origin": "https://music.apple.com",
        "Referer": "https://music.apple.com/",
        "User-Agent": UA,
    }


def fetch_developer_token(force: bool = False) -> str:
    """The Music web player's public developer token, cached."""
    with _dev_lock:
        fresh = (not force and _dev_token["value"]
                 and time.time() - _dev_token["at"] < _DEV_TOKEN_TTL)
        if fresh:
            return _dev_token["value"]
    try:
        _await_throttle()
        response = requests.get(WEB_PLAYER, headers={"User-Agent": UA},
                                timeout=TIMEOUT)
        if response.status_code == 429:
            # Scraping the player is the heaviest thing we do to Apple, so
            # a 429 here holds the catalog calls back as well.
            _record_error(WEB_PLAYER, response)
            hold_off(_retry_after(response, THROTTLE_BACKOFF))
            raise AppleError(
                "music.apple.com is rate limiting us (429) - wait a few "
                "minutes before trying again.\n%s" % describe_last_error())
        if not response.ok:
            _record_error(WEB_PLAYER, response)
            raise AppleError("music.apple.com returned %s\n%s" % (
                response.status_code, describe_last_error()))
        html = response.text
        token = ""
        found = _JWT.search(html)
        if found:
            token = found.group(0)
        else:
            for path in _ASSET.findall(html)[:12]:
                asset = requests.get("https://music.apple.com" + path,
                                     headers={"User-Agent": UA}, timeout=TIMEOUT)
                if not asset.ok:
                    continue
                found = _JWT.search(asset.text)
                if found:
                    token = found.group(0)
                    break
    except requests.RequestException as exc:
        raise AppleError("could not reach Apple Music: %s" % exc) from exc
    if not token:
        raise AppleError("no developer token found in the Music web player")
    with _dev_lock:
        _dev_token["value"] = token
        _dev_token["at"] = time.time()
    return token


def _retry_after(response, fallback: float) -> float:
    """How long Apple wants us to wait, or a usable guess.

    amp-api returns 429 with no Retry-After at all (code 42900, "Too Many
    Requests"/"Request is forbidden"), so the guess is what actually
    governs. It has to be a real pause: falling back to the first retry
    delay meant a whole budget of 1+2+4+8 seconds against a limit that
    lasts far longer, which just walked back into it four times.
    """
    stated = ""
    try:
        stated = (response.headers or {}).get("Retry-After", "")
    except Exception:
        stated = ""
    try:
        wait = float(stated) if stated else max(fallback, THROTTLE_BLIND_WAIT)
    except ValueError:
        wait = max(fallback, THROTTLE_BLIND_WAIT)
    return min(wait, THROTTLE_MAX_HOLD)


def hold_off(seconds: float) -> None:
    """Make every Apple call wait until the rate limit has passed.

    Per-request retries alone made a throttled scan worse, not better:
    with no pause between tracks, each of thousands of lookups hit the
    limit and then burned its whole retry budget against it, so being
    told to slow down multiplied the traffic instead of reducing it. The
    deadline is shared, so one 429 paces the entire run.
    """
    global _throttle_until
    with _throttle_lock:
        _throttle_until = max(_throttle_until,
                              time.time() + min(seconds, THROTTLE_MAX_HOLD))


def throttled_for() -> float:
    """Seconds left on the shared rate-limit hold, 0 when clear."""
    with _throttle_lock:
        return max(0.0, _throttle_until - time.time())


def _await_throttle() -> None:
    """Block until the shared deadline has actually passed.

    This slept once for at most THROTTLE_MAX_WAIT and then let the call
    through regardless, so a 60-second hold paused for 30 and went
    straight back into the limit. Sleeping in chunks keeps the cap on any
    single sleep while still honouring the whole deadline.
    """
    # Bounded: enough chunks to cover the longest possible hold, and no
    # more. Without a bound this spins forever against any sleep that
    # returns without the clock having moved.
    for _ in range(int(THROTTLE_MAX_HOLD // THROTTLE_MAX_WAIT) + 1):
        with _throttle_lock:
            remaining = _throttle_until - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, THROTTLE_MAX_WAIT))


# The last unsuccessful response, kept for diagnostics only: a status
# number on its own does not say whether a 429 came from Apple or from
# something in front of it, and the body usually does.
LAST_ERROR = {}
# Response headers worth keeping. An Authorization or Media-User-Token is
# never recorded - these are shown to the user and go into bug reports.
_KEEP_HEADERS = frozenset((
    "retry-after", "content-type", "server", "date", "via", "x-cache",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    # Present when Cloudflare answered instead of Apple, which is exactly
    # the case a bare status number cannot tell you about.
    "cf-ray", "cf-cache-status"))
_BODY_SNIPPET = 600


def _record_error(url: str, response) -> None:
    LAST_ERROR.clear()
    try:
        body = (response.text or "").strip()
    except Exception:
        body = "<unreadable>"
    LAST_ERROR.update(
        url=url.split("?")[0],
        status=response.status_code,
        body=body[:_BODY_SNIPPET],
        headers={name: value for name, value in
                 getattr(response, "headers", {}).items()
                 if name.lower() in _KEEP_HEADERS},
    )


def describe_last_error() -> str:
    """The last failing response as text, for the UI and bug reports."""
    if not LAST_ERROR:
        return ""
    parts = ["HTTP %s from %s" % (LAST_ERROR.get("status"), LAST_ERROR.get("url"))]
    for name, value in sorted((LAST_ERROR.get("headers") or {}).items()):
        parts.append("%s: %s" % (name, value))
    body = LAST_ERROR.get("body")
    parts.append("body: %s" % (body if body else "<empty>"))
    return "\n".join(parts)


def _get_json(url, headers, params=None):
    """One catalog call, waited out while Apple is asking us to slow down.

    A library scan runs these back to back, so 429 is a real outcome. It
    has to be waited out rather than returned: to every caller above,
    "no data" is indistinguishable from "this track has no lyrics", so a
    throttled scan would quietly mark thousands of tracks as misses.
    """
    delay = THROTTLE_BACKOFF
    for attempt in range(THROTTLE_RETRIES + 1):
        _await_throttle()
        try:
            response = requests.get(url, headers=headers, params=params,
                                    timeout=TIMEOUT)
        except requests.RequestException:
            return None, 0
        if response.status_code != 429:
            break
        _record_error(url, response)
        # Hold every other call back too, not just this one's retries.
        hold_off(_retry_after(response, delay))
        if attempt == THROTTLE_RETRIES:
            return None, 429
        delay = min(delay * 2, THROTTLE_MAX_WAIT)
    if not response.ok:
        _record_error(url, response)
        return None, response.status_code
    try:
        return response.json(), 200
    except ValueError:
        return None, 200


def _refresh_developer_token(stale: str) -> str:
    """A replacement for a developer token Apple has just rejected.

    Scraping the web player costs up to thirteen requests, and a scan
    calls this from every track, so it must not become a scrape per
    track. Two guards: if the cache already holds a different token some
    other call refreshed it, and a refresh that happened moments ago is
    not repeated - a 401 that survives a fresh token is the
    media-user-token being bad, which no amount of rescraping fixes.
    """
    global _last_refresh
    with _dev_lock:
        current = _dev_token["value"]
        if current and current != stale:
            return current
        if time.time() - _last_refresh < _REFRESH_COOLDOWN:
            return current
        _last_refresh = time.time()
    try:
        return fetch_developer_token(force=True)
    except AppleError:
        return ""


def _get_json_auth(url, developer_token: str, media_user_token: str,
                   params=None):
    """A catalog GET that survives the developer token rotating mid-scan.

    The token is cached for six hours but Apple rotates it on its own
    schedule, and a scan runs for longer than that. Without this, the
    moment it turns over every remaining track gets a 401 and is recorded
    as having no lyrics - a run that quietly goes empty half way through
    and still reports success.
    """
    data, status = _get_json(url, _headers(developer_token, media_user_token),
                             params)
    if status not in (401, 403):
        return data, status, developer_token
    fresh = _refresh_developer_token(developer_token)
    if not fresh or fresh == developer_token:
        return data, status, developer_token
    data, status = _get_json(url, _headers(fresh, media_user_token), params)
    return data, status, fresh


def search_song_row(developer_token: str, storefront: str, artist: str,
                    title: str, duration_seconds: Optional[int] = None):
    """The best-matching catalog song, as Apple returned it, or None.

    Deliberately anonymous - no media-user-token. Catalog search is
    public data and answers perfectly well without one, and Apple's rate
    limit is attached to the account rather than the address: a signed-in
    search and an anonymous one, same second and same address, came back
    429 and 200 respectively. Searching anonymously therefore spends none
    of the account's allowance, leaving all of it for the lyrics call
    that genuinely needs the subscription.
    """
    data, status, _ = _get_json_auth(
        SEARCH_URL % storefront, developer_token, "",
        {"term": ("%s %s" % (artist, title)).strip(), "types": "songs",
         "limit": "5"})
    if status in UNAVAILABLE_STATUS:
        raise AppleUnavailable("catalog search failed (status %s)" % status)
    if not data:
        return None
    try:
        songs = data["results"]["songs"]["data"]
    except (KeyError, TypeError):
        return None
    best, best_delta = None, None
    for song in songs:
        attributes = song.get("attributes") or {}
        millis = attributes.get("durationInMillis")
        if duration_seconds and millis:
            delta = abs(millis / 1000.0 - duration_seconds)
            if delta > DURATION_TOLERANCE:
                continue  # a different recording
        else:
            delta = float("inf")
        if best is None or delta < best_delta:
            best, best_delta = song, delta
    # Nothing agreed on duration: fall back to Apple's own ranking.
    if best is None and songs and not duration_seconds:
        best = songs[0]
    return best


def has_synced_lyrics(song) -> Optional[bool]:
    """Whether Apple says this song has time-synced lyrics.

    The search response carries hasLyrics and hasTimeSyncedLyrics without
    being asked, so a track Apple has no synced lyrics for can be dropped
    before spending the second request on it. None when Apple did not say
    - absence is not a no, so the caller should still ask.
    """
    attributes = (song or {}).get("attributes") or {}
    if "hasTimeSyncedLyrics" in attributes:
        return bool(attributes["hasTimeSyncedLyrics"])
    if attributes.get("hasLyrics") is False:
        return False
    return None


def search_song(developer_token: str, media_user_token: str, storefront: str,
                artist: str, title: str,
                duration_seconds: Optional[int] = None) -> Optional[str]:
    """The catalog id of the best match, or None.

    media_user_token is accepted and ignored: the search needs no account.
    """
    best = search_song_row(developer_token, storefront, artist, title,
                           duration_seconds)
    return (best or {}).get("id")


def fetch_ttml(developer_token: str, media_user_token: str, storefront: str,
               song_id: str) -> Optional[str]:
    data, status, _ = _get_json_auth(LYRICS_URL % (storefront, song_id),
                                     developer_token, media_user_token)
    if status in UNAVAILABLE_STATUS:
        raise AppleUnavailable("lyrics fetch failed (status %s)" % status)
    if not data:
        return None
    try:
        return data["data"][0]["attributes"]["ttml"]
    except (KeyError, IndexError, TypeError):
        return None


# -- TTML -> LRC ---------------------------------------------------------


def _parse_time(value) -> Optional[float]:
    """TTML clock values: "20.783", "1:20.783", "1:02:20.783"."""
    if not value:
        return None
    parts = str(value).strip().split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        return None
    return seconds


def _stamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return "%02d:%05.2f" % (minutes, seconds - minutes * 60)


def is_word_level(ttml: str) -> bool:
    return (re.search(r'timing="Word"', ttml or "", re.I) is not None)


def _leaf_spans(element) -> list:
    """Every span that actually carries a word, in document order."""
    found = []
    for span in element.findall(_TTML_NS + "span"):
        nested = span.findall(_TTML_NS + "span")
        found.extend(_leaf_spans(span) if nested else [span])
    return found


def _render_words(spans, default_begin: float) -> str:
    """Enhanced-LRC word tags, with time forced to run forwards.

    A word missing a begin, or carrying one earlier than the word before
    it, would make a player rewind mid-line; it inherits the running time
    instead.
    """
    pieces, running = [], default_begin
    for span in spans:
        word = "".join(span.itertext()).strip()
        if not word:
            continue
        at = _parse_time(span.get("begin"))
        if at is None or at < running:
            at = running
        running = at
        pieces.append("<%s>%s" % (_stamp(at), word))
    return " ".join(pieces)


def ttml_to_lrc(ttml: str, word_by_word: bool = False) -> Optional[str]:
    """Apple TTML to LRC.

    Line-level by default. With word_by_word, and when Apple supplied word
    timing, emits Enhanced (A2) LRC - still a valid LRC line, with inline
    <mm:ss.xx> tags before each word.
    """
    if not ttml:
        return None
    try:
        root = ET.fromstring(ttml)
    except ET.ParseError:
        return None

    want_words = word_by_word and is_word_level(ttml)
    lines = []
    for paragraph in root.iter(_TTML_NS + "p"):
        begin = _parse_time(paragraph.get("begin"))
        if begin is None:
            continue
        if not want_words:
            text = " ".join("".join(paragraph.itertext()).split())
            if text.strip():
                lines.append((begin, "[%s]%s" % (_stamp(begin), text)))
            continue

        # Apple nests background vocals in their own span group, whose
        # words are timed against the same stretch of the song as the main
        # line. Emitting them inline rewinds the clock mid-line, so each
        # group becomes its own LRC line at its own start instead.
        words, groups = [], []
        for span in paragraph.findall(_TTML_NS + "span"):
            if span.findall(_TTML_NS + "span"):
                groups.append(span)
            else:
                words.append(span)

        main = _render_words(words, begin) if words else " ".join(
            "".join(paragraph.itertext()).split())
        if main.strip():
            lines.append((begin, "[%s]%s" % (_stamp(begin), main)))

        for group in groups:
            leaves = _leaf_spans(group)
            start = _parse_time(group.get("begin"))
            if start is None and leaves:
                start = _parse_time(leaves[0].get("begin"))
            if start is None:
                start = begin
            rendered = _render_words(leaves, start)
            if rendered.strip():
                lines.append((start, "[%s]%s" % (_stamp(start), rendered)))

    if not lines:
        return None
    lines.sort(key=lambda pair: pair[0])
    return "\n".join(line for _, line in lines)


def check_token(media_user_token: str, storefront: str = "us") -> dict:
    """Is this media-user-token still good?

    A media-user-token is long-lived but not permanent: signing out,
    changing the password, or letting the subscription lapse invalidates
    it, and Apple then just refuses lyrics. Without an explicit check that
    shows up only as lyrics quietly never coming from Apple again, so the
    UI offers this and says which of the two tokens is at fault.
    """
    if not media_user_token:
        return {"ok": False, "detail": "no Apple media-user-token is set"}
    try:
        developer_token = fetch_developer_token(force=True)
    except AppleError as exc:
        return {"ok": False, "detail": "could not get a developer token: %s" % exc}

    headers = _headers(developer_token, media_user_token)
    data, status = _get_json(SEARCH_URL % storefront, headers,
                             {"term": "Billie Eilish lovely", "types": "songs",
                              "limit": "1"})
    if status in (401, 403):
        return {"ok": False, "detail":
                "Apple rejected the media-user-token (%s) - sign in again and "
                "copy a fresh one" % status}
    if status == 429:
        return {"ok": False, "detail":
                "Apple is rate limiting us (429). This is temporary and not a "
                "problem with your token - wait a few minutes, and avoid "
                "running a library scan at the same time.\n\nWhat Apple "
                "actually sent:\n%s" % describe_last_error()}
    if not data:
        return {"ok": False, "detail": "Apple search failed (status %s)\n\n%s" % (
            status, describe_last_error())}
    try:
        song_id = data["results"]["songs"]["data"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "detail": "unexpected search response from Apple"}

    _, status = _get_json(LYRICS_URL % (storefront, song_id), headers)
    if status == 200:
        return {"ok": True, "detail": "Apple Music lyrics are working"}
    if status in (401, 403):
        return {"ok": False, "detail":
                "search works but lyrics were refused (%s) - the account may "
                "not have an active subscription" % status}
    if status == 400:
        return {"ok": False, "detail":
                "Apple refused the request (400) - the developer token lacks "
                "lyrics permission"}
    return {"ok": False, "detail": "Apple returned status %s for lyrics" % status}


def fetch_synced(media_user_token: str, artist: str, title: str,
                 duration_seconds: Optional[int] = None,
                 storefront: str = "us",
                 word_by_word: bool = False,
                 wait: bool = True) -> Optional[str]:
    """The LRC for this track from Apple Music, or None when Apple has
    none for it. Raises AppleUnavailable when Apple could not be asked -
    a miss is a normal outcome, an unreachable service is not.

    wait=False gives up immediately while a rate-limit hold is in force
    instead of sitting out the minute. Worth it when another source can
    still answer: blocking there made a whole scan crawl at the length of
    Apple's hold even for tracks LRCLIB had all along. The upgrade pass
    passes wait=True, because for word timing Apple is the only source
    and skipping it would just report a false miss.
    """
    if not media_user_token or not artist or not title:
        return None
    if not wait:
        pause = throttled_for()
        if pause > 0:
            raise AppleUnavailable(
                "rate limited for another %ds; skipped so a source that can "
                "answer is not held up" % int(pause))
    try:
        developer_token = fetch_developer_token()
    except AppleError as exc:
        # No token means Apple was never asked, not that it had nothing.
        raise AppleUnavailable(str(exc)) from exc
    song = search_song_row(developer_token, storefront, artist, title,
                           duration_seconds)
    song_id = (song or {}).get("id")
    if not song_id:
        return None
    # No shortcut here. hasTimeSyncedLyrics looked like a free way to skip
    # the second request, but measured against a real library it was wrong
    # 9 times in 17: Apple flags a track as having no synced lyrics on an
    # anonymous search and then serves them when asked. A saved request is
    # not worth silently dropping lyrics we would otherwise have.
    ttml = fetch_ttml(developer_token, media_user_token, storefront, song_id)
    if not ttml:
        return None
    return ttml_to_lrc(ttml, word_by_word=word_by_word)
