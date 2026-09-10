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
    if args.tag_sources:
        # Offline and non-destructive, so it runs before the storage check
        # and needs no token: it only rewrites a header.
        from .backfill import tag_existing_sources
        found = tag_existing_sources(config.music_root,
                                     on_detail=lambda text: print(text))
        print("%d sidecar(s): %d already tagged, %d now tagged as Apple, "
              "%d left untagged" % (found.total, found.already, found.tagged,
                                    found.unknowable))
        if found.foreign:
            print("%d word-by-word file(s) have a .lrc.bak beside them, so "
                  "another tool converted those - left undetermined rather "
                  "than filed under Apple." % found.foreign)
        if found.unknowable:
            print("The untagged ones are line-level. LRCLIB, Musixmatch and "
                  "Apple all produce those and nothing in the file tells "
                  "them apart, so no source is written rather than a "
                  "plausible one. They get tagged when a pass rewrites them.")
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
    # The same database the web UI writes decisions into. Without it a CLI
    # scan matched everything afresh: a track identified by hand was looked
    # up again, and on an overwriting pass the hand-picked lyrics were
    # replaced by whatever matching chose. Refusals went unrecorded too, so
    # nothing scanned from here ever reached the review queue.
    from .db import Store
    store = Store(config.db_path)
    result = backfill_lyrics(config, on_detail=lambda text: print(text),
                             purge_bad=args.refresh, upgrade=args.upgrade,
                             redo_words=args.redo_words, store=store)
    if result.purged:
        print("removed %d placeholder lyric files" % result.purged)
    if args.redo_words:
        print("done: re-rendered %d of %d word-by-word tracks "
              "(%d left as they were)" % (
                  result.upgraded, result.total, result.no_match))
    elif args.upgrade:
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

    from . import __version__, apple

    print("beetdrop %s" % __version__)

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

    anonymous = dict(headers)
    anonymous.pop("Media-User-Token", None)

    def call(label: str, url: str, params=None, signed_in: bool = False) -> dict:
        """signed_in only where the account is genuinely needed - lyrics.

        Catalog metadata answers without one, and Apple's rate limit is on
        the account: sending the token needlessly is what made this probe
        report 429 while the code path it exists to test was working fine.
        """
        try:
            response = requests.get(url,
                                    headers=headers if signed_in else anonymous,
                                    params=params, timeout=apple.TIMEOUT)
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
                    {"ids": ",".join(ids), "include": rel}, signed_in=True)
        rows = data.get("data") or []
        carried = sum(1 for row in rows
                      if ((row.get("relationships") or {}).get(rel, {})
                          .get("data")))
        if rows:
            print("      %d of %d rows carried %s" % (carried, len(rows), rel))
            if carried:
                print("      -> lyrics can be batched; this is the big win")

    # Every track currently costs two calls: search for the id, then fetch
    # the lyrics. If the search can carry the lyrics itself that becomes
    # one - a bigger saving than batching, and it needs no id up front.
    print("\n== can the search itself carry the lyrics? ==")
    for parameter in ("include[songs]", "include"):
        for rel in ("syllable-lyrics", "lyrics"):
            data = call("GET /search&%s=%s" % (parameter, rel),
                        apple.SEARCH_URL % storefront,
                        {"term": query, "types": "songs", "limit": "1",
                         parameter: rel}, signed_in=True)
            rows = (((data or {}).get("results") or {}).get("songs")
                    or {}).get("data") or []
            carried = sum(1 for row in rows
                          if ((row.get("relationships") or {}).get(rel, {})
                              .get("data")))
            if rows:
                print("      %d of %d results carried %s" % (
                    carried, len(rows), rel))
                if carried:
                    print("      -> one request per track instead of two")

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


def cmd_plex_check(args, config: Config) -> int:
    """Would Plex's metadata find lyrics the file's own tags cannot?

    Plex has already matched every track against its own agent, so it
    holds a clean artist/title where a downloaded file may hold anything.
    Measured against *paths*, Plex looks far better - but Beetdrop reads
    embedded tags first and only falls back to the path, so that
    comparison flatters it. This reads the tags themselves.

    Read-only: nothing is fetched, nothing is written.
    """
    from . import __version__
    from .backfill import iter_audio_missing_lyrics, read_track_meta
    from .plexmeta import build_index, compare_tags, load_export

    print("beetdrop %s" % __version__)
    try:
        tracks = load_export(args.csv)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if not tracks:
        print("error: no track rows found in that export - it needs a "
              "locations column and a title column", file=sys.stderr)
        return 1
    index = build_index(tracks)
    print("read %d track(s) from Plex" % len(tracks))

    files = list(iter_audio_missing_lyrics(config.music_root)
                 if args.missing_only else
                 sorted(p for p in config.music_root.rglob("*")
                        if p.is_file() and p.suffix.lower() in
                        {".opus", ".m4a", ".mp3", ".flac", ".ogg", ".wav"}))
    if not files:
        print("no tracks to check")
        return 0
    print("checking %s of %d library file(s)..." % (
        args.sample if 0 < args.sample < len(files) else "all", len(files)))

    result = compare_tags(files, index, read_track_meta, sample=args.sample)
    print()
    print("%d checked, %d joined to a Plex row" % (result.checked, result.joined))
    if not result.joined:
        print("\nNothing joined. Plex's paths and this container's share no "
              "tail - check the export is for the same library.")
        return 2
    print("  tags already agree with Plex: %d" % result.same)
    print("  differ only in punctuation:   %d" % result.cosmetic)
    print("  same song, artist spelled differently: %d" % result.artist_only)
    print("  a genuinely different title:  %d" % result.different_title)
    print("  file has no usable tags:      %d" % result.untagged)
    print("\nPlex would change the search for %.0f%% of the joined tracks."
          % result.gain_pct)
    if result.examples:
        print("\nWhat that looks like:")
        for line in result.examples[:args.show]:
            print("  " + line)
    return 0


def cmd_richsync_raw(args, config: Config) -> int:
    """Show exactly what Musixmatch answers for one track.

    The first probe run reported that 100% of a library matched and 100%
    had richsync, and then nothing came back when the flag was tested.
    Numbers that clean are an instrument fault, not a result: Musixmatch
    returns a best-effort match for anything, and its real status hides
    inside the body while the HTTP request says 200 regardless. This
    prints both, so the difference between "not entitled" and "no word
    timing for this track" is visible.
    """
    import json as _json

    import requests

    from . import __version__
    from . import musixmatch as mxm

    print("beetdrop %s" % __version__)
    token = config.musixmatch_token
    if not token:
        try:
            token = mxm.fetch_token()
            print("fetched a fresh usertoken")
        except mxm.MusixmatchError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
    else:
        print("using the configured usertoken")

    params = {
        "format": "json", "namespace": "lyrics_richsynched",
        "subtitle_format": "lrc", "app_id": mxm.APP_ID, "usertoken": token,
        "q_track": args.title, "q_artist": args.artist,
    }
    if args.duration:
        params["q_duration"] = str(args.duration)
    print("\n--- macro.subtitles.get ---")
    response = requests.get(mxm.SUBTITLES_URL, params=params,
                            headers={"User-Agent": mxm.UA}, timeout=mxm.TIMEOUT)
    print("HTTP %s" % response.status_code)
    try:
        data = response.json()
    except ValueError:
        print("body is not JSON: %s" % response.text[:400])
        return 2
    print("musixmatch status %s" % mxm.inner_status(data))

    # The macro wraps several calls, each with its own status. A matcher
    # that failed while the envelope says 200 is how an unrelated track
    # came back looking like a match.
    calls = (((data.get("message") or {}).get("body") or {})
             .get("macro_calls") or {})
    if calls:
        print("macro calls:")
        for name in sorted(calls):
            print("  %-28s status %s" % (name, mxm.inner_status(calls[name])))
    else:
        print("no macro_calls in the response - not the shape expected")

    tracks = mxm._all_tracks(data)
    print("\ncandidate track objects found: %d" % len(tracks))
    for track in tracks[:5]:
        print("  id=%s  has_richsync=%s  has_subtitles=%s" % (
            track.get("track_id"), track.get("has_richsync"),
            track.get("has_subtitles")))
        print("    %s - %s  (%s ms)" % (
            track.get("artist_name"), track.get("track_name"),
            track.get("track_length")))
        print("    looks like what we asked for: %s" % (
            mxm.looks_like_the_track(args.artist, args.title, {
                "artist": track.get("artist_name"),
                "title": track.get("track_name")})))
    if not tracks:
        print("  none - nothing to ask for richsync")
        return 2

    # The line-level source rides this same call, so a wrong track here
    # is not only a richsync problem.
    subtitle = mxm._find_subtitle_body(data)
    print("\nline-level subtitle in the same response: %s" % (
        "yes, %d chars" % len(subtitle) if subtitle else "none"))
    if subtitle:
        print("  first line: %s" % subtitle.splitlines()[0][:90])

    track_id = tracks[0].get("track_id")
    # apic-desktop answered track.richsync.get with 404 "endpoint not
    # found", which is the server saying the route does not exist rather
    # than the track having no words. So try the spellings it might use
    # and report which, if any, it serves.
    payload, status = None, None
    for url, params in mxm.richsync_attempts(token, track_id):
        print("\n--- %s ---" % url.rsplit("/", 1)[-1])
        rich = requests.get(url, params=params,
                            headers={"User-Agent": mxm.UA}, timeout=mxm.TIMEOUT)
        print("HTTP %s" % rich.status_code)
        try:
            payload = rich.json()
        except ValueError:
            print("body is not JSON: %s" % rich.text[:300])
            continue
        status = mxm.inner_status(payload)
        hint = (((payload.get("message") or {}).get("header") or {})
                .get("hint") or "")
        print("musixmatch status %s%s" % (status, "  hint: %s" % hint if hint else ""))
        if status == 401:
            print("  -> the account is not entitled to richsync. A "
                  "subscription wall, not a gap in the catalogue.")
        elif hint == "endpoint not found":
            print("  -> this host does not serve that route at all.")
            continue
        elif status == 404:
            print("  -> served, but no richsync for this track.")
        if mxm._find_richsync_body(payload):
            break
    body = mxm._find_richsync_body(payload or {})
    if not body:
        print("no richsync_body in the response:")
        print("  " + _json.dumps(payload)[:500])
        return 0
    print("richsync_body: %d characters" % len(body))
    lrc = mxm.richsync_to_lrc(body)
    print("\nrendered:")
    for line in (lrc or "").splitlines()[:6]:
        print("  " + line)
    return 0


def cmd_richsync_probe(args, config: Config) -> int:
    """Is Musixmatch worth adding as a second word-by-word source?

    Apple is the only one today, so the question is not whether
    Musixmatch has word timing - it does, in its richsync tier - but how
    much of the part Apple already fails on it can cover. Measured on a
    sample before any of it is built, the way the Apple estimate was.
    """
    from . import __version__
    from .backfill import estimate_richsync_coverage

    # Printed so a run can be tied to a build. Without it, a result from
    # a stale image is indistinguishable from a fresh one.
    print("beetdrop %s" % __version__)
    est = estimate_richsync_coverage(config, sample=args.sample,
                                     verify=args.verify,
                                     on_detail=lambda text: print(text))
    print()
    if not est.population:
        print("nothing to measure: every track already has word timing")
        return 0
    if not est.checked:
        if not (est.deferred or est.skipped):
            # Never got as far as asking - the reason is already printed.
            print("Nothing was asked of Musixmatch; see the message above.")
        else:
            print("Musixmatch answered for none of the sample (%d deferred, "
                  "%d had no usable tags)" % (est.deferred, est.skipped))
        return 2

    print("Of %d tracks with no word timing, %d were sampled." % (
        est.population, est.checked))
    print("  Musixmatch matched:      %d (%.0f%%)" % (est.matched, est.match_pct))
    if est.wrong_match:
        print("  answered with a different song: %d (not counted as matches)"
              % est.wrong_match)
    print("  of those, has richsync:  %d (%.0f%%)" % (est.claimed, est.pct))
    print("  share of everything asked: %.0f%% +/- %.0f" % (
        est.pct_of_all, est.margin))
    print("  projected across all %d: about %d tracks" % (
        est.population, est.projected))
    if est.deferred:
        print("  (%d deferred, excluded from the figures)" % est.deferred)

    if not est.fetched:
        print("\nNothing was flagged as covered, so the flag was never tested.")
    else:
        print("\nThe has_richsync flag was checked for real on %d of them: %d "
              "returned word timing (%.0f%%)." % (
                  est.fetched, est.verified, est.flag_pct))
        if est.verified < est.fetched:
            print("A flag that is wrong is worse than no flag - treat the "
                  "projection above as an upper bound.")
    if est.samples:
        print("\nWhat came back, to check the timing by eye:")
        for text in est.samples[:args.verify]:
            print("\n  " + text.replace("\n", "\n  "))
    if args.show_matches and est.matched_examples:
        print("\nWhat Musixmatch matched each track to:")
        for line in est.matched_examples:
            print("  " + line)
    if est.wrong_match_files:
        print("\nAnswered with something else:")
        for line in est.wrong_match_files[:10]:
            print("  " + line)
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


def cmd_apple_tokens(args, config: Config) -> int:
    """Show every developer token the web player offers and which the
    catalog API accepts.

    The web player ships several JWTs for different Apple services. Only
    one works against the catalog API, and the others are refused with
    429 "Request is forbidden" - which reads as a rate limit and is not
    one. This says plainly which is which, from this machine, so a wrong
    token and a genuinely limited address can be told apart.
    """
    import base64
    import json

    from . import __version__, apple

    print("beetdrop %s" % __version__)
    try:
        candidates = apple._candidate_tokens()
    except apple.AppleError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if not candidates:
        print("no developer token found in the web player at all")
        return 1
    print("found %d distinct token(s)\n" % len(candidates))

    working = []
    for index, token in enumerate(candidates, 1):
        payload = {}
        try:
            middle = token.split(".")[1]
            middle += "=" * (-len(middle) % 4)
            payload = json.loads(base64.urlsafe_b64decode(middle))
        except Exception:
            pass
        print("--- token %d of %d ---" % (index, len(candidates)))
        print("    %s...%s" % (token[:18], token[-8:]))
        for field in ("iss", "exp", "root_https_origin"):
            if field in payload:
                print("    %-18s %s" % (field, payload[field]))
        ok = apple._token_works(token)
        print("    catalog search     %s" % ("ACCEPTED" if ok else "refused"))
        if ok:
            working.append(index)
        print()

    if working:
        print("%d of %d accepted (token %s). Beetdrop will use one of these."
              % (len(working), len(candidates),
                 ", ".join(str(i) for i in working)))
        return 0
    print("Every token was refused from this machine. Since a token that "
          "works elsewhere is refused here, this really is the address "
          "being limited rather than the wrong token being sent.")
    return 2


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
    p_scan.add_argument("--redo-words", action="store_true",
                        help="re-fetch every .lrc that already has per-word "
                             "timing and write it again, to repair sidecars "
                             "left behind by an older rendering")
    p_scan.add_argument("--tag-sources", action="store_true",
                        help="write a source tag onto sidecars that predate "
                             "it. Offline, and it never guesses: word-level "
                             "files are provably Apple, line-level ones are "
                             "left untagged")
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

    p_rich = sub.add_parser(
        "richsync-probe",
        help="measure how much of the library Musixmatch could serve "
             "word-by-word, on the tracks Apple has already failed on")
    p_rich.add_argument("--sample", type=int, default=100,
                        help="how many tracks to ask about (0 = all, "
                             "default 100)")
    p_rich.add_argument("--verify", type=int, default=5,
                        help="how many of the tracks flagged as covered to "
                             "actually fetch and render (default 5)")
    p_rich.add_argument("--show-matches", action="store_true",
                        help="print what Musixmatch matched each track to, "
                             "so a wrong match is visible")
    p_rich.set_defaults(func=cmd_richsync_probe)

    p_plex = sub.add_parser(
        "plex-check",
        help="compare your files' own tags against a Plex CSV export, to "
             "see whether Plex's metadata would find lyrics the tags cannot")
    p_plex.add_argument("csv", nargs="+", help="Plex CSV export file(s)")
    p_plex.add_argument("--sample", type=int, default=200,
                        help="how many library files to read tags from "
                             "(0 = all, default 200)")
    p_plex.add_argument("--show", type=int, default=15,
                        help="how many differences to print (default 15)")
    p_plex.add_argument("--missing-only", action="store_true", default=True,
                        help="only tracks with no .lrc yet (the default)")
    p_plex.add_argument("--all-tracks", dest="missing_only",
                        action="store_false",
                        help="check every track, not only those missing lyrics")
    p_plex.set_defaults(func=cmd_plex_check)

    p_rraw = sub.add_parser(
        "richsync-raw",
        help="show exactly what Musixmatch answers for one track, "
             "including its own status code, which the HTTP status hides")
    p_rraw.add_argument("artist")
    p_rraw.add_argument("title")
    p_rraw.add_argument("--duration", type=int, default=0)
    p_rraw.set_defaults(func=cmd_richsync_raw)

    p_tokens = sub.add_parser(
        "apple-tokens",
        help="list the developer tokens the web player offers and show "
             "which the catalog API accepts")
    p_tokens.set_defaults(func=cmd_apple_tokens)

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
