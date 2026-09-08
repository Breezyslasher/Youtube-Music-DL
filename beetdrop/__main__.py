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
    from .backfill import backfill_lyrics, lyrics_stats
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
        return 0
    print("done: added lyrics to %d of %d tracks missing them "
          "(%d no match, %d skipped)" % (
              result.added, result.total, result.no_match, result.skipped))
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

    p_serve = sub.add_parser("serve", help="run the web API")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8090)
    p_serve.set_defaults(func=cmd_serve)

    p_version = sub.add_parser("version", help="show beetdrop and yt-dlp versions")
    p_version.set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    config = Config()
    if getattr(args, "library", None):
        config.music_root = Path(args.library).expanduser()
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
