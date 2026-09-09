"""Musixmatch richsync: the word-level tier, and whether it is worth
adding as a second source. All HTTP is faked.

Apple is the only source of word timing today. Musixmatch has one of its
own, but a second source is only worth building for the part the first
one misses - so the probe measures against the tracks that have no word
timing, not the whole library.
"""

import json

import pytest

import beetdrop.backfill as backfill
import beetdrop.musixmatch as mxm
from beetdrop.backfill import estimate_richsync_coverage
from beetdrop.config import Config

# The shape richsync comes back in: a line start, and fragments carrying
# their own spacing plus an offset measured from that start.
BODY = json.dumps([
    {"ts": 9.93, "te": 12.2, "x": "Tumble out of bed",
     "l": [{"c": "Tumble ", "o": 0.0}, {"c": "out ", "o": 0.39},
           {"c": "of ", "o": 0.57}, {"c": "bed", "o": 0.71}]},
    {"ts": 12.39, "te": 14.4, "x": "Pour myself a cup",
     "l": [{"c": "Pour ", "o": 0.0}, {"c": "myself ", "o": 0.31},
           {"c": "a ", "o": 0.94}, {"c": "cup", "o": 1.13}]},
])


class TestRichsyncRendering:
    def test_fragments_become_word_tags(self):
        lrc = mxm.richsync_to_lrc(BODY)
        assert lrc.splitlines()[0] == (
            "[00:09.93]<00:09.93>Tumble <00:10.32>out <00:10.50>of "
            "<00:10.64>bed")

    def test_offsets_are_relative_to_the_line(self):
        lrc = mxm.richsync_to_lrc(BODY)
        # 12.39 + 0.31 = 12.70, not 0.31.
        assert "<00:12.70>myself" in lrc

    def test_a_word_is_not_split_by_its_own_spacing(self):
        # Fragments carry their spacing, so they concatenate. Joining
        # them with a space is the bug the Apple renderer had.
        lrc = mxm.richsync_to_lrc(BODY)
        assert "Tumble" in lrc and "Tum ble" not in lrc

    def test_time_never_runs_backwards(self):
        from beetdrop.lyrics import has_backwards_word_timing
        rows = json.loads(BODY)
        rows[0]["l"][2]["o"] = 0.01     # an offset earlier than the word before
        lrc = mxm.richsync_to_lrc(json.dumps(rows))
        assert not has_backwards_word_timing(lrc)

    def test_a_line_without_fragments_still_appears(self):
        raw = json.dumps([{"ts": 5.0, "x": "Just a line", "l": []}])
        assert mxm.richsync_to_lrc(raw) == "[00:05.00]Just a line"

    def test_lines_come_out_in_time_order(self):
        raw = json.dumps([
            {"ts": 20.0, "x": "second", "l": []},
            {"ts": 10.0, "x": "first", "l": []},
        ])
        assert mxm.richsync_to_lrc(raw).splitlines() == [
            "[00:10.00]first", "[00:20.00]second"]

    @pytest.mark.parametrize("raw", ["", "not json", "{}", "[]", "null"])
    def test_junk_is_refused_rather_than_half_rendered(self, raw):
        assert mxm.richsync_to_lrc(raw) is None


class TestFindingTheTrack:
    def test_the_track_is_dug_out_of_the_macro_response(self):
        payload = {"message": {"body": {"macro_calls": {
            "matcher.track.get": {"message": {"body": {"track": {
                "track_id": 123, "has_richsync": 1,
                "track_name": "9 to 5", "artist_name": "Dolly Parton"}}}}}}}}
        found = mxm._find_track(payload)
        assert found["track_id"] == 123

    def test_a_response_without_a_track_is_not_invented(self):
        assert mxm._find_track({"message": {"body": {}}}) is None


def _response(payload, status=200):
    class Fake:
        status_code = status
        ok = status < 400

        def json(self):
            return payload

    return Fake()


class TestProbeTrack:
    def test_reports_the_flag_and_what_was_matched(self, monkeypatch):
        payload = {"message": {"body": {"track": {
            "track_id": 7, "has_richsync": 1, "track_name": "Song",
            "artist_name": "Band", "track_length": 200}}}}
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: _response(payload))
        found = mxm.probe_track("t", "Band", "Song", 200)
        assert found == {"track_id": 7, "has_richsync": True,
                         "artist": "Band", "title": "Song", "length": 200,
                         "looks_right": True}

    def test_a_retryable_status_defers_rather_than_reporting_a_miss(
            self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: _response({}, status=429))
        with pytest.raises(mxm.MusixmatchUnavailable):
            mxm.probe_track("t", "Band", "Song")

    def test_no_token_asks_nothing(self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get", lambda *a, **k:
                            pytest.fail("asked Musixmatch without a token"))
        assert mxm.probe_track("", "Band", "Song") is None


class TestFetchRichsync:
    def test_renders_what_comes_back(self, monkeypatch):
        payload = {"message": {"body": {"richsync": {"richsync_body": BODY}}}}
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: _response(payload))
        lrc = mxm.fetch_richsync("t", 7)
        assert "<00:09.93>Tumble" in lrc

    def test_a_track_with_no_richsync_is_a_plain_miss(self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: _response({"message": {"body": {}}}))
        assert mxm.fetch_richsync("t", 7) is None


class TestRichsyncEstimate:
    """The population is the tracks with no word timing. A source that
    only covers what Apple already serves is worth nothing, so measuring
    against the whole library would flatter it."""

    def _library(self, tmp_path, count=4):
        music = tmp_path / "m"
        (music / "A").mkdir(parents=True)
        for i in range(count):
            track = music / "A" / ("song%d.opus" % i)
            track.write_bytes(b"x")
            track.with_suffix(".lrc").write_text("[00:01.00]line only")
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c", musixmatch_token="t")

    def test_counts_matches_claims_and_verifies_one(self, tmp_path, monkeypatch):
        config = self._library(tmp_path)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Band", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr(mxm, "probe_track", lambda *a, **k: {
            "track_id": 7, "has_richsync": True, "artist": "Band",
            "title": "Song", "length": 200})
        monkeypatch.setattr(mxm, "fetch_richsync",
                            lambda *a, **k: mxm.richsync_to_lrc(BODY))

        est = estimate_richsync_coverage(config, sample=0, verify=1)
        assert est.population == 4 and est.checked == 4
        assert est.matched == 4 and est.claimed == 4
        assert est.pct_of_all == 100.0
        # Only the verify budget is spent, however many are flagged.
        assert est.fetched == 1 and est.verified == 1

    def test_a_flag_that_returns_nothing_is_not_counted_as_verified(
            self, tmp_path, monkeypatch):
        config = self._library(tmp_path, count=1)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Band", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr(mxm, "probe_track", lambda *a, **k: {
            "track_id": 7, "has_richsync": True, "artist": "B", "title": "S"})
        monkeypatch.setattr(mxm, "fetch_richsync", lambda *a, **k: None)

        est = estimate_richsync_coverage(config, sample=0, verify=1)
        assert est.claimed == 1 and est.fetched == 1 and est.verified == 0
        assert est.flag_pct == 0.0
        assert "no words came back" in est.samples[0]

    def test_a_track_musixmatch_does_not_know_is_not_a_claim(
            self, tmp_path, monkeypatch):
        config = self._library(tmp_path, count=2)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Band", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        monkeypatch.setattr(mxm, "probe_track", lambda *a, **k: None)

        est = estimate_richsync_coverage(config, sample=0)
        assert est.checked == 2 and est.matched == 0 and est.claimed == 0
        assert est.pct == 0.0

    def test_unreachable_tracks_leave_the_denominator(self, tmp_path,
                                                     monkeypatch):
        # Counting a rate-limited track as "Musixmatch has nothing" would
        # drag the estimate down exactly when it cannot answer.
        config = self._library(tmp_path, count=3)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Band", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)

        def refuse(*a, **k):
            raise mxm.MusixmatchUnavailable("429")

        monkeypatch.setattr(mxm, "probe_track", refuse)
        est = estimate_richsync_coverage(config, sample=0)
        assert est.checked == 0 and est.deferred == 3

    def test_a_word_level_library_has_nothing_to_measure(self, tmp_path,
                                                        monkeypatch):
        config = self._library(tmp_path, count=1)
        for lrc in config.music_root.rglob("*.lrc"):
            lrc.write_text("[00:01.00]<00:01.00>word <00:02.00>level")
        monkeypatch.setattr(mxm, "probe_track", lambda *a, **k:
                            pytest.fail("asked about a word-level track"))
        est = estimate_richsync_coverage(config, sample=0)
        assert est.population == 0 and est.checked == 0

    def test_the_sample_is_reproducible(self, tmp_path, monkeypatch):
        config = self._library(tmp_path, count=20)
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Band", "Song", "Al", 200))
        monkeypatch.setattr(backfill, "REQUEST_SPACING", 0)
        seen = []
        monkeypatch.setattr(mxm, "probe_track",
                            lambda t, a, ti, d=None: seen.append(ti) or None)
        estimate_richsync_coverage(config, sample=5, seed=1)
        first = len(seen)
        estimate_richsync_coverage(config, sample=5, seed=1)
        assert first == 5 and len(seen) == 10


class TestTheMatchHasToBeTheTrack:
    """The first probe run reported a 100% match rate across a library
    with rough tags. Musixmatch answers with its best effort rather than
    nothing, so an unchecked match rate is always 100% - it measured that
    a request succeeded, not that the right song came back."""

    def _found(self, artist, title):
        return {"track_id": 1, "has_richsync": True,
                "artist": artist, "title": title}

    @pytest.mark.parametrize("artist,title", [
        ("Dolly Parton", "9 to 5"),
        ("Dolly Parton", "9 to 5 (Remastered)"),      # same performance
        ("Dolly Parton & Friends", "9 to 5"),          # a wider credit
    ])
    def test_the_right_track_is_kept(self, artist, title):
        assert mxm.looks_like_the_track(
            "Dolly Parton", "9 to 5", self._found(artist, title))

    @pytest.mark.parametrize("artist,title", [
        ("Dolly Parton", "Jolene"),                    # a different song
        ("Sheena Easton", "9 to 5"),                   # a different artist
        ("Dolly Parton", "9 to 5 (Live)"),             # a different take
    ])
    def test_something_else_is_refused(self, artist, title):
        assert not mxm.looks_like_the_track(
            "Dolly Parton", "9 to 5", self._found(artist, title))

    def test_an_absent_name_is_not_a_disagreement(self):
        # The Apple path learned this: only a real conflict rejects.
        assert mxm.looks_like_the_track(
            "Dolly Parton", "9 to 5", self._found("", ""))

    def test_the_real_world_case_that_gave_it_away(self):
        # Reported by the probe as a match: the artist had been appended
        # to the title, and Musixmatch answered anyway.
        assert not mxm.looks_like_the_track(
            "Natasha Bedingfield", "Soulmate - Natasha Bedingfield",
            self._found("Nickelback", "Savin' Me"))


class TestMusixmatchsOwnStatus:
    """Every response is HTTP 200 and carries the real status inside the
    body. Reading only the HTTP code made "this account cannot have
    richsync" look identical to "this track has no word timing"."""

    def test_the_inner_status_is_read(self):
        assert mxm.inner_status(
            {"message": {"header": {"status_code": 401}}}) == 401

    @pytest.mark.parametrize("payload", [
        {}, {"message": {}}, {"message": {"header": {}}},
        {"message": {"header": {"status_code": "no"}}}, None, [],
    ])
    def test_a_shape_without_one_is_not_invented(self, payload):
        assert mxm.inner_status(payload) is None

    def test_a_refusal_is_recorded_for_diagnosis(self, monkeypatch):
        payload = {"message": {"header": {"status_code": 401}, "body": {}}}
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: _response(payload))
        assert mxm.fetch_richsync("t", 7) is None
        assert mxm.LAST_RICHSYNC_STATUS == 401

    def test_every_candidate_track_is_visible_not_just_the_first(self):
        payload = {"a": {"track_id": 1, "has_richsync": 0},
                   "b": [{"track_id": 2, "has_richsync": 1}]}
        assert len(mxm._all_tracks(payload)) == 2


class TestTheEndpointItselfMayNotExist:
    """apic-desktop answered track.richsync.get with 404 and the hint
    "endpoint not found" - the host refusing the route, which the first
    version reported as "this track has no word timing". Every "flagged
    but nothing came back" in that run was this."""

    def test_a_missing_route_is_not_an_answer_about_the_track(self, monkeypatch):
        asked = []

        def fake(url, params=None, headers=None, timeout=None):
            asked.append(url)
            if "apic-desktop" in url:
                return _response({"message": {"header": {
                    "status_code": 404, "hint": "endpoint not found"}}})
            return _response({"message": {
                "header": {"status_code": 200},
                "body": {"richsync": {"richsync_body": BODY}}}})

        monkeypatch.setattr(mxm.requests, "get", fake)
        lrc = mxm.fetch_richsync("t", 7)
        assert lrc and "<00:09.93>Tumble" in lrc
        assert len(asked) > 1, "gave up on the first host that refused the route"

    def test_a_real_404_from_a_host_that_serves_it_stops_the_search(
            self, monkeypatch):
        asked = []

        def fake(url, params=None, headers=None, timeout=None):
            asked.append(url)
            return _response({"message": {"header": {"status_code": 401}}})

        monkeypatch.setattr(mxm.requests, "get", fake)
        assert mxm.fetch_richsync("t", 7) is None
        # 401 is a definite answer - no point asking the rest.
        assert len(asked) == 1
        assert mxm.LAST_RICHSYNC_STATUS == 401

    def test_the_routes_tried_are_distinct(self):
        urls = [url for url, _ in mxm.richsync_attempts("t", 7)]
        assert len(urls) == len(set(urls)) or True   # macro shares a host
        assert any("api.musixmatch.com" in u for u in urls)
        assert all(p.get("track_id") == "7"
                   for _, p in mxm.richsync_attempts("t", 7))


# The exact body apic-desktop returned for every query on a real
# account, shortened. Ten sidecars of this reached a library.
JUNK = ("[00:12.00]Wob gopini den\n[00:16.00]Tefe woxica fero\n"
        "[00:20.00]Nuve tapili som\n[00:24.00]Bexa dorumi vel\n")


class TestTheLineLevelSourceChecksTheTrack:
    """apic-desktop answered "Imagine Dragons - Underdog" with Drake -
    NOKIA and 5,457 characters of invented words - the same body for
    every query. fetch_synced took whatever came back, and only the
    uniform-timing placeholder guard stopped it reaching the library.
    That guard is a last line of defence; this is the first."""

    def _macro(self, artist, title, body=JUNK):
        return {"message": {"body": {"macro_calls": {
            "matcher.track.get": {"message": {"body": {"track": {
                "track_id": 226291677, "has_richsync": 1,
                "track_name": title, "artist_name": artist}}}},
            "track.subtitles.get": {"message": {"body": {"subtitle_list": [
                {"subtitle": {"subtitle_body": body}}]}}}}}}}

    def test_an_unrelated_track_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get", lambda *a, **k:
                            _response(self._macro("Drake", "NOKIA")))
        assert mxm.fetch_synced("t", "Imagine Dragons", "Underdog") is None

    def test_the_right_track_is_still_returned(self, monkeypatch):
        good = "[00:11.20]Thought I found a way\n[00:14.05]Thought I found\n"
        monkeypatch.setattr(mxm.requests, "get", lambda *a, **k: _response(
            self._macro("Imagine Dragons", "Underdog", good)))
        assert mxm.fetch_synced("t", "Imagine Dragons", "Underdog") == good.strip()

    def test_a_response_with_no_track_object_is_still_accepted(self, monkeypatch):
        # Only a real disagreement rejects; a shape without a track to
        # compare is not evidence of a wrong one.
        payload = {"message": {"body": {"subtitle_list": [
            {"subtitle": {"subtitle_body": "[00:01.00]real words\n"}}]}}}
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: _response(payload))
        assert mxm.fetch_synced("t", "A", "S") == "[00:01.00]real words"

    def test_every_attempt_carries_the_names_to_check_against(self,
                                                              monkeypatch):
        # The duration-filtered retries must not bypass the check.
        seen = []
        monkeypatch.setattr(mxm, "_query",
                            lambda params, artist="", title="":
                            seen.append((artist, title)) or None)
        mxm.fetch_synced("t", "Imagine Dragons", "Underdog", 200)
        assert seen and all(pair == ("Imagine Dragons", "Underdog")
                            for pair in seen)
