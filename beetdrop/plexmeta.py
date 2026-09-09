"""Plex's view of the library, read from a CSV export.

Plex has already matched every track against its own metadata agent, so
it holds a clean artist/title where the file may hold whatever a tagger
or a download left behind. That is exactly what an Apple search wants.

Nothing here fetches or writes anything. It exists to answer one
question with evidence rather than assumption: for the tracks we cannot
find lyrics for, is Plex's metadata actually better than what the file
already carries? Beetdrop prefers embedded tags and only falls back to
the path, so a comparison against paths alone would flatter Plex.

The join is on the file path, which is exact - but Plex's paths are from
Plex's mount and ours are from the container's, so they are matched by
their longest shared tail rather than in full.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# How many trailing path components to try when joining. Four reaches
# past Artist/Album/Track, which is enough to separate same-named files
# in different albums.
SUFFIX_DEPTH = 4


@dataclass
class PlexTrack:
    path: str
    artist: str
    title: str
    album: str


def _column(fieldnames, *endings) -> Optional[str]:
    """The first column whose name ends with one of these.

    Plex exports nest differently depending on what was exported, so
    "albums.tracks.title" and "title" both have to work.
    """
    for ending in endings:
        for name in fieldnames or []:
            if name == ending or name.endswith("." + ending):
                return name
    return None


def load_export(paths) -> list:
    """Every track row in one or more Plex CSV exports."""
    found = []
    for name in paths:
        with open(name, newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            location = _column(fields, "locations", "file")
            title = _column(fields, "tracks.title", "title")
            artist = _column(fields, "tracks.grandparentTitle",
                             "grandparentTitle", "originalTitle")
            album = _column(fields, "tracks.parentTitle", "parentTitle")
            if not (location and title):
                continue
            for row in reader:
                where = (row.get(location) or "").strip()
                # A track can list more than one file; each is a row for us.
                for one in [part.strip() for part in where.splitlines() if part.strip()]:
                    if not (row.get(title) or "").strip():
                        continue
                    found.append(PlexTrack(
                        path=one,
                        artist=(row.get(artist) or "").strip() if artist else "",
                        title=(row.get(title) or "").strip(),
                        album=(row.get(album) or "").strip() if album else ""))
    return found


def _suffixes(path: str):
    parts = [part for part in Path(path).parts if part not in ("/", "\\")]
    for depth in range(1, min(SUFFIX_DEPTH, len(parts)) + 1):
        yield "/".join(parts[-depth:]).lower()


def build_index(tracks) -> dict:
    """Trailing path fragments to the tracks that end with them.

    Plex sees /srv/.../Media/Music/A/B.mp3 where the container sees
    /music/A/B.mp3, so nothing joins on the whole path.
    """
    index: dict = {}
    for track in tracks:
        for suffix in _suffixes(track.path):
            index.setdefault(suffix, []).append(track)
    return index


def lookup(index: dict, path) -> Optional[PlexTrack]:
    """The one Plex track this file is, or None if it is ambiguous.

    Longest tail first, and a fragment shared by several tracks is no
    answer at all - a wrong join would be worse than no join.
    """
    best = None
    for suffix in _suffixes(str(path)):
        found = index.get(suffix)
        if found and len(found) == 1:
            best = found[0]
    return best


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass
class TagComparison:
    """How the file's own tags compare with what Plex holds."""
    checked: int = 0
    joined: int = 0          # matched to a Plex row at all
    untagged: int = 0        # no artist/title in the file
    same: int = 0            # tags already say what Plex says
    cosmetic: int = 0        # differ only in punctuation or case
    artist_only: int = 0     # same song, artist spelled differently
    different_title: int = 0  # a different title - what breaks a search
    examples: list = field(default_factory=list)

    @property
    def gain_pct(self) -> float:
        """Share of joined tracks Plex would usefully change.

        Only a different title and an untagged file count. An artist
        spelled differently - "98 Degrees" against "98°" - is a real
        difference in the string but rarely in the outcome, because
        artist comparison normalises and allows 0.75 similarity. Counting
        it here would inflate the case for building this.
        """
        useful = self.different_title + self.untagged
        return 100.0 * useful / self.joined if self.joined else 0.0


def compare_tags(tracks, index: dict, read_meta, sample: int = 0,
                 seed: Optional[int] = None) -> TagComparison:
    """Read each file's own tags and see whether Plex disagrees usefully.

    read_meta is injected so this stays testable without audio files.
    """
    import random

    result = TagComparison()
    chosen = (list(tracks) if sample <= 0 or sample >= len(tracks)
              else random.Random(seed).sample(list(tracks), sample))
    for path in chosen:
        result.checked += 1
        theirs = lookup(index, path)
        if theirs is None:
            continue
        result.joined += 1
        meta = read_meta(path) or ("", "", "", None)
        artist, title = meta[0] or "", meta[1] or ""
        if not (artist and title):
            # Nothing to compare, and the case Plex helps most.
            result.untagged += 1
            if len(result.examples) < 40:
                result.examples.append(
                    "untagged  %s\n          plex: %s - %s"
                    % (Path(path).name, theirs.artist, theirs.title))
            continue
        if (artist, title) == (theirs.artist, theirs.title):
            result.same += 1
        elif _key(artist) == _key(theirs.artist) and _key(title) == _key(theirs.title):
            result.cosmetic += 1
        elif _key(title) == _key(theirs.title):
            result.artist_only += 1
        else:
            result.different_title += 1
            if len(result.examples) < 40:
                result.examples.append(
                    "tags:     %s - %s\n          plex: %s - %s"
                    % (artist, title, theirs.artist, theirs.title))
    return result
