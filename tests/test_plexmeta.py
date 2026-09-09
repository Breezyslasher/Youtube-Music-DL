"""Reading a Plex export and joining it to the library on disk.

Plex has matched every track against its own agent, so it holds a clean
artist/title where a downloaded file may hold anything. Whether that is
worth using depends on a number nobody had: how often the file's *own
tags* are already right. Measured against paths alone, Plex looks far
better than it is, because Beetdrop reads tags first.
"""

from pathlib import Path

import pytest

from beetdrop.plexmeta import (
    PlexTrack,
    build_index,
    compare_tags,
    load_export,
    lookup,
)

# The shape a real export has: deeply prefixed column names, and the
# artist under grandparentTitle rather than anything called "artist".
HEADER = ("ratingKey,albums.tracks.locations,albums.tracks.title,"
          "albums.tracks.grandparentTitle,albums.tracks.parentTitle\n")


def _export(tmp_path, rows, header=HEADER):
    path = tmp_path / "export.csv"
    path.write_text(header + "".join(rows), encoding="utf-8")
    return str(path)


class TestReadingTheExport:
    def test_reads_path_artist_title_and_album(self, tmp_path):
        csv = _export(tmp_path, [
            '1,/srv/Media/Music/explo/Bye_Bye_Bye-_NSYNC.mp3,'
            'Bye Bye Bye,*NSYNC,Bye Bye Bye\n'])
        tracks = load_export([csv])
        assert len(tracks) == 1
        assert tracks[0].artist == "*NSYNC"
        assert tracks[0].title == "Bye Bye Bye"

    def test_rows_with_no_file_or_no_title_are_dropped(self, tmp_path):
        # A real export is sparse: parent rows repeat with blank children.
        csv = _export(tmp_path, [
            '1,,,,\n',
            '2,/m/a.mp3,,Artist,Album\n',
            '3,/m/b.mp3,Real,Artist,Album\n'])
        assert [t.title for t in load_export([csv])] == ["Real"]

    def test_a_track_with_two_files_becomes_two_rows(self, tmp_path):
        csv = _export(tmp_path, [
            '1,"/m/a.mp3\n/m/b.mp3",Song,Artist,Album\n'])
        assert len(load_export([csv])) == 2

    def test_an_export_without_the_nesting_prefix_still_works(self, tmp_path):
        csv = _export(tmp_path, ['1,/m/a.mp3,Song,Artist,Album\n'],
                      header="ratingKey,locations,title,grandparentTitle,"
                             "parentTitle\n")
        assert load_export([csv])[0].title == "Song"

    def test_an_export_with_no_usable_columns_yields_nothing(self, tmp_path):
        csv = _export(tmp_path, ['1,2\n'], header="ratingKey,summary\n")
        assert load_export([csv]) == []

    def test_several_exports_combine(self, tmp_path):
        one = _export(tmp_path, ['1,/m/a.mp3,A,Artist,Album\n'])
        two = tmp_path / "two.csv"
        two.write_text(HEADER + '2,/m/b.mp3,B,Artist,Album\n', encoding="utf-8")
        assert len(load_export([one, str(two)])) == 2


class TestJoiningToTheLibrary:
    """Plex's paths come from Plex's mount and ours from the container's,
    so nothing joins on the whole string."""

    def _index(self, *paths):
        return build_index([PlexTrack(path=p, artist="A", title="T", album="")
                            for p in paths])

    def test_a_different_mount_point_still_joins(self):
        index = self._index("/srv/dev-disk-1/Media/Music/Dolly/9 to 5.mp3")
        assert lookup(index, "/music/Dolly/9 to 5.mp3") is not None

    def test_an_ambiguous_tail_is_no_answer(self):
        # Two albums with the same track name: a wrong join would be
        # worse than none, so neither is offered.
        index = self._index("/srv/Music/A/Live/Song.mp3",
                            "/srv/Music/A/Studio/Song.mp3")
        assert lookup(index, "/music/Song.mp3") is None

    def test_more_of_the_path_disambiguates(self):
        index = self._index("/srv/Music/A/Live/Song.mp3",
                            "/srv/Music/A/Studio/Song.mp3")
        found = lookup(index, "/music/A/Studio/Song.mp3")
        assert found is not None and "Studio" in found.path

    def test_a_file_plex_does_not_know_joins_to_nothing(self):
        assert lookup(self._index("/srv/Music/A/B.mp3"), "/music/X/Y.mp3") is None

    def test_the_join_ignores_case(self):
        index = self._index("/srv/Music/Dolly/9 To 5.mp3")
        assert lookup(index, "/music/dolly/9 to 5.mp3") is not None


class TestComparingAgainstTheTags:
    """The number that decides whether this is worth building."""

    def _setup(self, plex, tags):
        index = build_index([PlexTrack(path=p, artist=a, title=t, album="")
                             for p, a, t in plex])

        def read_meta(path):
            return tags.get(Path(path).name)

        return index, read_meta

    def test_tags_that_already_agree_are_not_a_gain(self):
        index, read = self._setup(
            [("/srv/Music/A/song.mp3", "Dolly Parton", "9 to 5")],
            {"song.mp3": ("Dolly Parton", "9 to 5", "Al", 200)})
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.joined == 1 and result.same == 1
        assert result.gain_pct == 0.0

    def test_punctuation_only_differences_are_not_a_gain(self):
        index, read = self._setup(
            [("/srv/Music/A/song.mp3", "Dolly Parton", "I’ll Give It All")],
            {"song.mp3": ("Dolly Parton", "I'll Give It All", "Al", 200)})
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.cosmetic == 1 and result.different_title == 0
        assert result.gain_pct == 0.0

    def test_a_differently_spelled_artist_is_not_counted_as_a_gain(self):
        # "98 Degrees" against "98°" is a real difference in the string
        # and rarely one in the outcome: artist comparison normalises and
        # allows 0.75 similarity. Counting it would inflate the case.
        index, read = self._setup(
            [("/srv/Music/A/song.mp3", "98°", "Chance To Love You More")],
            {"song.mp3": ("98 Degrees", "Chance To Love You More", "Al", 200)})
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.artist_only == 1 and result.different_title == 0
        assert result.gain_pct == 0.0

    def test_a_real_difference_counts(self):
        # The case that motivated this: the file is named for the film.
        index, read = self._setup(
            [("/srv/Music/A/song.mp3", "The American Film Orchestra",
              "A Whole New World (Aladdin)")],
            {"song.mp3": ("The American Film Orchestra", "Aladdin", "Al", 200)})
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.different_title == 1 and result.gain_pct == 100.0

    def test_an_untagged_file_is_where_plex_helps_most(self):
        index, read = self._setup(
            [("/srv/Music/A/song.mp3", "*NSYNC", "Bye Bye Bye")],
            {"song.mp3": ("", "", "", 200)})
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.untagged == 1 and result.gain_pct == 100.0
        assert "plex: *NSYNC - Bye Bye Bye" in result.examples[0]

    def test_a_file_plex_does_not_know_is_not_counted_either_way(self):
        index, read = self._setup(
            [("/srv/Music/A/other.mp3", "A", "B")],
            {"song.mp3": ("Tagged", "Song", "Al", 200)})
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.checked == 1 and result.joined == 0
        assert result.gain_pct == 0.0

    def test_unreadable_tags_are_treated_as_untagged_not_as_agreement(self):
        index, read = self._setup(
            [("/srv/Music/A/song.mp3", "A", "B")], {})   # read_meta -> None
        result = compare_tags([Path("/music/A/song.mp3")], index, read)
        assert result.untagged == 1

    def test_the_sample_is_reproducible(self):
        plex = [("/srv/Music/A/s%d.mp3" % i, "A", "T") for i in range(20)]
        tags = {"s%d.mp3" % i: ("A", "T", "", 1) for i in range(20)}
        index, read = self._setup(plex, tags)
        files = [Path("/music/A/s%d.mp3" % i) for i in range(20)]
        first = compare_tags(files, index, read, sample=5, seed=7)
        again = compare_tags(files, index, read, sample=5, seed=7)
        assert first.checked == again.checked == 5
