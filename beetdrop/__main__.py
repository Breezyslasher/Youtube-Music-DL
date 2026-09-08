"""CLI.

    python -m beetdrop search "query"
    python -m beetdrop grab <video_id> [--format opus|m4a|mp3]
    python -m beetdrop serve [--host 0.0.0.0] [--port 8090]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import SUPPORTED_FORMATS, Config, StorageError, check_storage
from .download import DownloadError, ytdlp_version
from .grab import run_album_grab, run_grab, run_video_grab
from .search import search_albums, search_songs, search_videos
from .settings import config_with_settings


def _format_duration(seconds) -> str:
    if seconds is None:
        return "?:??"
    return "%d:%02d" % divmod(int(seconds), 60)


def cmd_search(args, config: Config) -> int:
    if args.albums:
        albums = search_albums(args.query, limit=args.limit)
        if not albums:
            print("no results")
            return 1
        for album in albums:
            print("%s  %s - %s (%s%s)" % (
                album.browse_id,
                album.artist_display or "?",
                album.title,
                album.year or "?",
                (", " + album.album_type) if album.album_type else "",
            ))
        return 0
    search = search_videos if args.videos else search_songs
    results = search(args.query, limit=args.limit)
    if not results:
        print("no results")
        return 1
    for result in results:
        album = result.album or "no album"
        print("%s  %-6s %s - %s [%s]" % (
            result.video_id,
            _format_duration(result.duration_seconds),
            result.artist_display or "?",
            result.title,
            album,
        ))
    return 0


def cmd_grab(args, config: Config) -> int:
    try:
        if args.video:
            config.video_root.mkdir(parents=True, exist_ok=True)
            check_storage(config.video_root, config.min_free_mb)
            outcome = run_video_grab(
                args.video_id, config,
                on_stage=lambda stage: print("stage: %s" % stage),
                on_resolved=lambda r: print("resolved: %s - %s (%ss)" % (
                    r.artist_display or "?", r.title, r.duration_seconds)),
            )
            print("done: %s filed to %s" % (
                "matched" if outcome.verified else "unmatched", outcome.inbox_path))
            return 0
        check_storage(config.music_root, config.min_free_mb)
        if args.album:
            outcome = run_album_grab(
                args.video_id, config,
                fmt=args.format or "", bitrate=args.bitrate or "",
                on_stage=lambda stage: print("stage: %s" % stage),
                on_resolved=lambda title, artist: print("resolved: %s - %s" % (artist, title)),
                on_detail=lambda text: print(text),
            )
            if outcome.failed:
                for failure in outcome.failed:
                    print("failed: track %s - %s: %s" % (
                        failure.get("n", "?"), failure.get("title", "?"),
                        failure.get("reason", "?")), file=sys.stderr)
            print("done: %d tracks filed to %s" % (outcome.delivered, outcome.inbox_path))
            return 0
        outcome = run_grab(
            args.video_id, config,
            fmt=args.format or "", bitrate=args.bitrate or "",
            on_stage=lambda stage: print("stage: %s" % stage),
            on_resolved=lambda r: print("resolved: %s - %s (%ss)" % (
                r.artist_display or "?", r.title, r.duration_seconds)),
        )
    except (DownloadError, ValueError, StorageError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("done: filed to %s" % outcome.inbox_path)
    return 0


def cmd_scan_lyrics(args, config: Config) -> int:
    from .backfill import backfill_lyrics, estimate_word_coverage, lyrics_stats
    if args.estimate is not None:
        if not config.apple_token:
            print("error: no Apple media-user-token configured, and Apple is "
                  "the only source of word timing", file=sys.stderr)
            return 1
        est = estimate_word_coverage(config, sample=args.estimate,
                                     on_detail=lambda text: print(text))
        if not est.population:
            print("nothing to upgrade: no line-level tracks found")
            return 0
        if not est.checked:
            print("\nApple answered for none of the sample (%d deferred, "
                  "%d had no usable tags) - try again when the rate limit "
                  "clears" % (est.deferred, est.skipped))
            return 2
        print("\nApple recognised %d of the %d tracks asked about (%.0f%%)."
              % (est.matched, est.checked, est.match_pct))
        if est.matched:
            print("Of the ones it recognised, %.0f%% (+/- %.0f) have "
                  "word-by-word." % (est.pct, est.margin))
        print("Across all %d tracks an upgrade would visit, that is about "
              "%d tracks improved." % (est.population, est.projected))
        if est.not_found:
            print("\n%d of the sample matched nothing at all. Those are "
                  "metadata, not Apple: better tags would find some of them."
                  % est.not_found)
            for name in est.not_found_files[:10]:
                print("    no match: %s" % name)
        if est.skipped:
            print("  %d had no usable artist/title to search with" % est.skipped)
        if est.deferred:
            print("  %d could not be checked (rate limit or network); the "
                  "estimate ignores them" % est.deferred)
        if args.show_matches:
            print("\nwhat Apple matched your tracks to:")
            for line in est.matched_examples:
                print("    %s" % line)
        elif est.matched_examples:
            print("\nspot-check a few matches (--show-matches for more):")
            for line in est.matched_examples[:5]:
                print("    %s" % line)
        return 0
    if args.list:
        # Uncapped, one path per line, so it can be piped or grepped.
        stats = lyrics_stats(config.music_root, sample=0)
        bucket = {"line": stats.line_level_files,
                  "missing": stats.missing_files,
                  "placeholder": stats.placeholder_files,
                  "broken": stats.backwards_files}[args.list]
        try:
            for name in bucket:
                print(name)
            sys.stdout.flush()
        except BrokenPipeError:
            # `... --list broken | head` closes the pipe early. That is the
            # intended use, not an error, so exit quietly - and detach
            # stdout so the interpreter does not retry the flush at exit.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    if args.stats:
        s = lyrics_stats(config.music_root)
        print("%d of %d tracks have lyrics (%.1f%%)" % (
            s.with_lyrics, s.audio_files, s.coverage_pct))
        print("  word-by-word: %d (%.1f%% of those with lyrics)" % (
            s.word_level, s.word_pct))
        print("  line-level:   %d" % s.line_level)
        print("  no lyrics:    %d" % s.missing)
        if s.backwards:
            print("  of the word-by-word, %d have word timing that runs "
                  "backwards mid-line and need --upgrade" % s.backwards)
        if s.placeholder:
            print("  placeholder junk still present: %d" % s.placeholder)
        return 0
    try:
        check_storage(config.music_root, config.min_free_mb)
    except StorageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    result = backfill_lyrics(config, on_detail=lambda text: print(text),
                             purge_bad=args.refresh, upgrade=args.upgrade)
    if result.purged:
        print("removed %d placeholder lyric files" % result.purged)
    if args.upgrade:
        print("done: upgraded %d of %d line-level tracks to word-by-word "
              "(%d had no word-level version)" % (
                  result.upgraded, result.total, result.no_match))
    else:
        print("done: added lyrics to %d of %d tracks missing them "
              "(%d no match, %d skipped)" % (
                  result.added, result.total, result.no_match, result.skipped))
    if result.deferred:
        # Not a miss: these were never answered, so the run is incomplete
        # and saying "done" without this would be a lie.
        print("%d track(s) could not be checked (rate limit, token, or "
              "network) - run the pass again to retry them" % result.deferred)
        return 2
    return 0


def cmd_lyrics_probe(args, config: Config) -> int:
    """Explain, for one track, what each lyric source actually returns.

    Answers the question the scan cannot: is a track line-level because
    Apple has no word timing for it, or because Apple was never reached?
    """
    from . import apple
    from .lyrics import _lrclib, _musixmatch, fetch_synced_lyrics, has_word_timing

    artist, title = args.artist, args.title
    duration = args.duration
    print("query: artist=%r title=%r duration=%s" % (artist, title, duration))
    print("settings: provider=%s word_by_word=%s apple_token=%s" % (
        config.lyrics_provider, config.word_lyrics,
        "set" if config.apple_token else "NOT SET"))

    print("\n-- Apple --")
    if not config.apple_token:
        print("  no media-user-token configured, so Apple is never asked")
    else:
        try:
            developer = apple.fetch_developer_token()
            storefront = config.apple_storefront or "us"
            found, _ = apple._get_json(
                apple.SEARCH_URL % storefront,
                apple._headers(developer, config.apple_token),
                {"term": ("%s %s" % (artist, title)).strip(),
                 "types": "songs", "limit": "5"})
            songs = (((found or {}).get("results") or {}).get("songs") or {}).get("data") or []
            if not songs:
                print("  catalog search found nothing")
            for song in songs:
                attributes = song.get("attributes") or {}
                seconds = (attributes.get("durationInMillis") or 0) / 1000.0
                delta = ("%+.1fs" % (seconds - duration)) if duration else "n/a"
                print("  candidate: %-38r by %-24r %5.1fs (%s vs your file)" % (
                    attributes.get("name"), attributes.get("artistName"),
                    seconds, delta))
            chosen = apple.search_song(developer, config.apple_token, storefront,
                                       artist, title, duration)
            if not chosen:
                print("  CHOSEN: none - every candidate was outside the %ds "
                      "duration tolerance" % apple.DURATION_TOLERANCE)
            else:
                print("  CHOSEN: id %s" % chosen)
                ttml = apple.fetch_ttml(developer, config.apple_token,
                                        storefront, chosen)
                if not ttml:
                    print("  lyrics: none for that id")
                else:
                    print("  lyrics: timing=%s" % (
                        "WORD (Apple has word-by-word)" if apple.is_word_level(ttml)
                        else "Line (Apple has no word timing for this track)"))
        except Exception as exc:
            print("  error: %s: %s" % (type(exc).__name__, exc))

    print("\n-- other sources --")
    lrclib = _lrclib(artist, title, "", duration)
    print("  lrclib:     %s" % ("hit (line-level)" if lrclib else "no match"))
    mxm = _musixmatch(artist, title, "", duration, config.musixmatch_token)
    print("  musixmatch: %s" % (
        "hit (line-level)" if mxm else
        "no match" if config.musixmatch_token else "no token configured"))

    print("\n-- what Beetdrop would write, with your current settings --")
    final = fetch_synced_lyrics(
        artist, title, "", duration,
        musixmatch_token=config.musixmatch_token,
        provider=config.lyrics_provider,
        apple_token=config.apple_token,
        apple_storefront=config.apple_storefront,
        word_by_word=config.word_lyrics)
    if not final:
        print("  nothing - no source had synced lyrics")
    else:
        kind = "WORD-BY-WORD" if has_word_timing(final) else "line-level"
        print("  %s, %d lines" % (kind, len(final.splitlines())))
        for line in final.splitlines()[:3]:
            print("    %s" % line[:110])
    return 0


def cmd_apple_raw(args, config: Config) -> int:
    """Show exactly what Apple returns, unfiltered.

    A status number alone does not say whether a 429 is Apple rate
    limiting the account or something in front of it - a proxy, a CDN
    block page - answering on Apple's behalf. The body says which, so
    this prints it verbatim: no retries, no backoff, no interpretation.
    """
    import requests

    from . import apple

    def show(label: str, response) -> None:
        print("\n=== %s ===" % label)
        print("HTTP %s %s" % (response.status_code, response.reason or ""))
        for name, value in sorted(response.headers.items()):
            print("  %s: %s" % (name, value))
        body = (response.text or "").strip()
        print("--- body (%d bytes) ---" % len(body))
        print(body[:args.bytes] if body else "<empty>")
        if len(body) > args.bytes:
            print("... truncated, %d more bytes" % (len(body) - args.bytes))

    # 1. The web player scrape. This is the first thing the Settings test
    #    does and the heaviest call we make, so it is the likeliest 429.
    try:
        show("GET %s (developer token scrape)" % apple.WEB_PLAYER,
             requests.get(apple.WEB_PLAYER,
                          headers={"User-Agent": apple.UA},
                          timeout=apple.TIMEOUT))
    except Exception as exc:
        print("\n=== %s ===\nrequest failed: %s: %s" % (
            apple.WEB_PLAYER, type(exc).__name__, exc))

    if not config.apple_token:
        print("\nNo media-user-token configured, so the catalog call is "
              "skipped. Sign in on the Settings page first.")
        return 1

    # 2. The catalog search, with the real token pair.
    try:
        developer = apple.fetch_developer_token()
    except apple.AppleError as exc:
        print("\ncould not get a developer token, so the catalog call is "
              "skipped:\n%s" % exc)
        return 1
    storefront = config.apple_storefront or "us"
    try:
        show("GET catalog search (storefront=%s)" % storefront,
             requests.get(apple.SEARCH_URL % storefront,
                          headers=apple._headers(developer, config.apple_token),
                          params={"term": "Billie Eilish lovely",
                                  "types": "songs", "limit": "1"},
                          timeout=apple.TIMEOUT))
        # 3. The same search with the developer token only. Catalog search
        #    is public data, so this needs no account - and that is the
        #    point: if it succeeds while the call above is refused, the
        #    limit is attached to the media-user-token or its account, and
        #    signing in again may clear it. If both are refused, it is the
        #    address being limited and only time will.
        anonymous = dict(apple._headers(developer, ""))
        anonymous.pop("Media-User-Token", None)
        show("GET catalog search WITHOUT the media-user-token",
             requests.get(apple.SEARCH_URL % storefront, headers=anonymous,
                          params={"term": "Billie Eilish lovely",
                                  "types": "songs", "limit": "1"},
                          timeout=apple.TIMEOUT))
    except Exception as exc:
        print("\ncatalog request failed: %s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


def cmd_apple_explore(args, config: Config) -> int:
    """Test whether Apple offers a cheaper route than search-then-fetch.

    An upgrade costs two calls per track: a text search for the song id,
    then the lyrics fetch. Both halves might be improvable - the search
    replaced by an exact ISRC lookup, and either half batched across many
    songs - but only Apple can say which of those it actually supports.
    So ask it, against the real token, and report what came back.
    """
    import requests

    from . import apple
    from .backfill import (AUDIO_EXTS, isrc_coverage, read_isrc,
                           read_track_meta, tidy_track_name)

    if not config.apple_token:
        print("error: no Apple media-user-token configured", file=sys.stderr)
        return 1
    storefront = config.apple_storefront or "us"
    try:
        developer = apple.fetch_developer_token()
    except apple.AppleError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    headers = apple._headers(developer, config.apple_token)

    def call(label: str, url: str, params=None) -> dict:
        try:
            response = requests.get(url, headers=headers, params=params,
                                    timeout=apple.TIMEOUT)
        except Exception as exc:
            print("  %-46s request failed: %s" % (label, exc))
            return {}
        ok = response.status_code == 200
        print("  %-46s HTTP %s" % (label, response.status_code))
        if not ok:
            print("      %s" % (response.text or "")[:200])
            return {}
        try:
            return response.json() or {}
        except ValueError:
            return {}

    # 1. How many files could even use an ISRC lookup.
    print("== ISRC tags in your library ==")
    with_isrc, checked = isrc_coverage(config.music_root, limit=args.sample)
    print("  %d of %d files sampled carry an ISRC (%.0f%%)" % (
        with_isrc, checked, 100.0 * with_isrc / checked if checked else 0))
    if not with_isrc:
        print("  -> no ISRC lookup possible as things stand; the other "
              "tests below do not need one")

    # 2. Collect real material to probe with: song ids via the normal
    #    search, and an ISRC from a tagged file.
    #
    #    One search, taken raw. search_song retries through the whole
    #    backoff - four minutes of silence on a rate-limited account - and
    #    a diagnostic must never do that: here the status code IS the
    #    answer, so it is reported rather than waited out. One search also
    #    returns several songs, so three ids cost one request, not three.
    ids, isrc, tried = [], "", 0
    query = ""
    for path in sorted(config.music_root.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTS):
            continue
        if not isrc and tried < args.sample:
            isrc = read_isrc(path)
        tried += 1
        if not query:
            artist, title, _, _ = read_track_meta(path) or ("", "", "", None)
            if artist and title:
                query = "%s %s" % (artist, tidy_track_name(title, artist))
        if query and (isrc or tried >= args.sample):
            break
    if not query:
        print("\nno track with both an artist and a title tag to search with")
        return 1

    print("\nresolving song ids with one search for %r ..." % query[:60])
    found = call("GET /search", apple.SEARCH_URL % storefront,
                 {"term": query, "types": "songs", "limit": "5"})
    songs_found = (((found or {}).get("results") or {}).get("songs")
                   or {}).get("data") or []
    ids = [s.get("id") for s in songs_found if s.get("id")][:3]
    if not ids:
        print("\nNothing below can be tested without a song id. If the status "
              "above was 429 this is the rate limit, not a fault in your "
              "setup - wait a few minutes and run it again.")
        return 2
    print("probing with song ids %s%s" % (
        ", ".join(ids), (" and ISRC %s" % isrc) if isrc else " (no ISRC found)"))

    songs = "https://amp-api.music.apple.com/v1/catalog/%s/songs" % storefront

    print("\n== can one request cover several songs? ==")
    data = call("GET /songs?ids=a,b,c", songs, {"ids": ",".join(ids)})
    got = len((data.get("data") or []))
    print("      returned %d of %d songs" % (got, len(ids)))
    if got == len(ids):
        print("      -> batching works; the lookup half can be shared")

    print("\n== can lyrics come back in that same request? ==")
    for rel in ("lyrics", "syllable-lyrics"):
        data = call("GET /songs?ids=...&include=%s" % rel, songs,
                    {"ids": ",".join(ids), "include": rel})
        rows = data.get("data") or []
        carried = sum(1 for row in rows
                      if ((row.get("relationships") or {}).get(rel, {})
                          .get("data")))
        if rows:
            print("      %d of %d rows carried %s" % (carried, len(rows), rel))
            if carried:
                print("      -> lyrics can be batched; this is the big win")

    print("\n== can an exact ISRC replace the text search? ==")
    if not isrc:
        print("  skipped: no ISRC tag found to test with")
    else:
        data = call("GET /songs?filter[isrc]=...", songs, {"filter[isrc]": isrc})
        rows = data.get("data") or []
        print("      returned %d song(s)" % len(rows))
        if rows:
            attrs = rows[0].get("attributes") or {}
            print("      -> %r by %r" % (attrs.get("name"),
                                         attrs.get("artistName")))
            print("      -> exact lookup works, and cannot match the wrong "
                  "recording the way a text search can")

    print("\n== does a song say whether it has lyrics, without fetching? ==")
    data = call("GET /songs/{id}?extend=hasTimeSyncedLyrics",
                "%s/%s" % (songs, ids[0]), {"extend": "hasTimeSyncedLyrics"})
    attrs = ((data.get("data") or [{}])[0].get("attributes") or {})
    flags = {k: v for k, v in attrs.items() if "yric" in k}
    print("      lyric-related attributes: %s" % (flags or "none"))
    if flags:
        print("      -> tracks without lyrics could be skipped before the "
              "second call")
    return 0


def cmd_verify_skip(args, config: Config) -> int:
    """Check whether Apple's has-synced-lyrics flag can be trusted.

    fetch_synced skips the lyrics request when the catalog search says a
    track has no synced lyrics. That halves the cost, and rests entirely
    on the flag being accurate on an anonymous search - which was assumed
    from one working example, not established. This makes the skipped
    request anyway and reports whether anything was being lost.
    """
    from .backfill import verify_skip_flag

    if not config.apple_token:
        print("error: no Apple media-user-token configured", file=sys.stderr)
        return 1
    check = verify_skip_flag(config, sample=args.sample,
                             on_detail=lambda text: print(text))
    print()
    if not check.flagged_false:
        print("Apple did not flag any of the %d tracks searched as having no "
              "synced lyrics, so the skip never fired here and this says "
              "nothing either way. Try a larger --sample." % check.searched)
        return 0
    print("%d of %d tracks searched were flagged 'no synced lyrics'."
          % (check.flagged_false, check.searched))
    if not check.flag_is_wrong:
        print("Every one of them really had none: the flag is trustworthy on "
              "an anonymous search, and the skip is safe.")
        return 0
    print("%d of those %d had lyrics anyway (%.0f%%), %d of them word-by-word."
          % (check.had_lyrics, check.flagged_false, check.wrong_pct,
             check.had_word))
    print("The flag is NOT trustworthy and the skip is losing lyrics. "
          "It should be removed.")
    for line in check.wrong_examples:
        print("    flagged as having none, actually had: %s" % line)
    return 3


def cmd_serve(args, config: Config) -> int:
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_version(args, config: Config) -> int:
    from . import __version__
    print("beetdrop %s (yt-dlp %s)" % (__version__, ytdlp_version()))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="beetdrop")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="search YouTube Music songs or albums")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--albums", action="store_true", help="search albums instead of songs")
    p_search.add_argument("--videos", action="store_true", help="search music videos instead of songs")
    p_search.set_defaults(func=cmd_search)

    p_grab = sub.add_parser("grab", help="download one video or album and file it into the library")
    p_grab.add_argument("video_id", help="videoId, or an album browseId with --album")
    p_grab.add_argument("--album", action="store_true", help="treat the id as an album browseId")
    p_grab.add_argument("--video", action="store_true",
                        help="grab as a music video (mp4 + Kodi .nfo + poster)")
    p_grab.add_argument("--format", choices=SUPPORTED_FORMATS, help="output format (default opus)")
    p_grab.add_argument("--bitrate", help="bitrate for mp3 transcodes")
    p_grab.add_argument("--library", help="music library path (overrides MUSIC_PATH)")
    p_grab.set_defaults(func=cmd_grab)

    p_scan = sub.add_parser("scan-lyrics",
                            help="fetch synced lyrics for library tracks missing them")
    p_scan.add_argument("--refresh", action="store_true",
                        help="delete placeholder .lrc files first, then re-fetch them")
    p_scan.add_argument("--list", choices=("line", "missing", "placeholder", "broken"),
                        help="print every track in that bucket, one per line: "
                             "line = has lyrics but no word-by-word, "
                             "broken = word timing that runs backwards mid-line")
    p_scan.add_argument("--estimate", type=int, nargs="?", const=100,
                        metavar="N",
                        help="ask Apple about N random line-level tracks "
                             "(default 100) and report what share of the "
                             "library it could serve word-by-word, without "
                             "writing anything")
    p_scan.add_argument("--show-matches", action="store_true",
                        help="with --estimate, print what Apple matched each "
                             "track to, so a wrong match is visible")
    p_scan.add_argument("--stats", action="store_true",
                        help="report lyric coverage and how much is word-by-word, "
                             "without fetching anything")
    p_scan.add_argument("--upgrade", action="store_true",
                        help="re-fetch tracks whose .lrc has no per-word timing "
                             "and replace it when Apple has a word-level version")
    p_scan.set_defaults(func=cmd_scan_lyrics)

    p_probe = sub.add_parser(
        "lyrics-probe",
        help="show what each lyric source returns for one track, and whether "
             "Apple has word-by-word for it")
    p_probe.add_argument("artist")
    p_probe.add_argument("title")
    p_probe.add_argument("--duration", type=int, default=0,
                         help="track length in seconds; Apple matches within "
                              "8s, so passing it explains a missed match")
    p_probe.set_defaults(func=cmd_lyrics_probe)

    p_raw = sub.add_parser(
        "apple-raw",
        help="print Apple's raw response - status, headers and body - so a "
             "429 can be checked for what it actually is")
    p_raw.add_argument("--bytes", type=int, default=2000,
                       help="how much of each body to print (default 2000)")
    p_raw.set_defaults(func=cmd_apple_raw)

    p_explore = sub.add_parser(
        "apple-explore",
        help="test whether Apple supports batching, ISRC lookup, or a "
             "has-lyrics flag, any of which would cut the two calls a track "
             "currently costs")
    p_explore.add_argument("--sample", type=int, default=300,
                           help="how many files to check for ISRC tags "
                                "(default 300; 0 for the whole library)")
    p_explore.set_defaults(func=cmd_apple_explore)

    p_verify = sub.add_parser(
        "verify-skip",
        help="check whether Apple's has-synced-lyrics flag is accurate, "
             "since a wrong flag means the lyrics fetch is being skipped "
             "for tracks that do have lyrics")
    p_verify.add_argument("--sample", type=int, default=30,
                          help="how many flagged tracks to verify (default 30)")
    p_verify.set_defaults(func=cmd_verify_skip)

    p_serve = sub.add_parser("serve", help="run the web API")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8090)
    p_serve.set_defaults(func=cmd_serve)

    p_version = sub.add_parser("version", help="show beetdrop and yt-dlp versions")
    p_version.set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    # `docker exec` gives a pipe, not a terminal, and Python block-buffers
    # to a pipe: a scan's progress lines sit in an 8KB buffer instead of
    # appearing, so a long command looks hung when it is working fine.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # The same settings the web UI saves - tokens, provider, word-by-word.
    # Without this the CLI ran on environment variables alone, so
    # scan-lyrics --upgrade found no Apple token and did nothing at all.
    config = config_with_settings(Config())
    if getattr(args, "library", None):
        config.music_root = Path(args.library).expanduser()
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
