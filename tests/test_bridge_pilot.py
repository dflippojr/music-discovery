"""`scripts/bridge_pilot.py` on synthetic records and catalogs; no network."""

import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import bridge_pilot as pilot  # noqa: E402

PLAN = pilot.load_plan()


def record(
    base: float = 1.0, length: float = 200.0, artist: str = "A", title: str = "S"
) -> dict:
    scalar = {"mean": base, "median": base + 0.1, "var": base + 0.2}
    return {
        "metadata": {
            "audio_properties": {"length": length},
            "tags": {"artist": [artist], "title": [title]},
        },
        "lowlevel": {
            "mfcc": {
                "mean": [base + i for i in range(13)],
                "cov": [
                    [base + i if i == j else 0.5 for j in range(13)] for i in range(13)
                ],
            },
            "spectral_centroid": scalar,
            "spectral_rolloff": scalar,
            "spectral_rms": scalar,
            "zerocrossingrate": scalar,
        },
    }


def catalog(tracks_per_genre: int = 8) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    genres = ["Rock", "Pop"]
    rows = len(genres) * tracks_per_genre
    frame = pd.DataFrame(
        {
            "track_id": np.arange(100, 100 + rows),
            "genre_top": np.repeat(genres, tracks_per_genre),
            "source_url": [
                f"http://freemusicarchive.org/music/t{i}" for i in range(rows)
            ],
        }
    )
    features = pd.DataFrame(
        rng.normal(size=(rows, 38)), columns=pilot.feature_columns()
    )
    return pd.concat([frame, features], axis=1)


def known_tracks(count: int = 4) -> list[dict]:
    return [
        {
            "artist": f"Artist {i}",
            "title": f"Song {i}",
            "genre": "Rock" if i % 2 else "Pop",
        }
        for i in range(count)
    ]


class FakeClient:
    """Answers `Client.get` from URL fragments, and counts the calls."""

    def __init__(self, recordings: dict[str, list[dict]], available: dict[str, dict]):
        self.recordings = recordings
        self.available = available

    def get(self, url: str):
        if "musicbrainz" in url:
            hits = [v for k, v in self.recordings.items() if k.replace(" ", "+") in url]
            return {"recordings": hits[0] if hits else []}
        if "/count" in url:
            return {mbid: {"0": {}} for mbid in self.available if mbid in url}
        return self.available.get(url.split("/")[-2])


def hit(mbid: str, title: str, artist: str, **extra) -> dict:
    return {"id": mbid, "title": title, "artist-credit": [{"name": artist}], **extra}


def test_plan_is_thirty_tracks_with_alternates_and_a_catalog_genre():
    genres = {"Electronic", "Experimental", "Folk", "Hip-Hop", "Instrumental"}
    genres |= {"International", "Pop", "Rock"}
    assert len(PLAN["tracks"]) == PLAN["pairs"] == 30
    assert PLAN["pass_wins"] == 21
    assert {track["genre"] for track in PLAN["tracks"]} <= genres
    songs = [(t["artist"], t["title"]) for t in PLAN["tracks"] + PLAN["alternates"]]
    assert len(set(songs)) == len(songs)
    assert {t["genre"] for t in PLAN["tracks"]} == {
        t["genre"] for t in PLAN["alternates"]
    }


def test_rank_candidates_keeps_this_song_and_prefers_the_plain_recording():
    track = {"artist": "Queen", "title": "Bohemian Rhapsody"}
    ranked = pilot.rank_candidates(
        track,
        [
            hit("live", "Bohemian Rhapsody", "Queen", disambiguation="live at Wembley"),
            hit("note", "Bohemian Rhapsody", "Queen", disambiguation="mono"),
            hit(
                "date", "Bohemian Rhapsody", "Queen", disambiguation="1973-01-22: Hall"
            ),
            hit("cover", "Bohemian Rhapsody", "Queensryche"),
            hit("other", "Another One", "Queen"),
            hit("duet", "Bohemian Rhapsody", "Queen & Friends"),
            hit("plain", "BOHEMIAN  rhapsody", "Queen"),
        ],
    )
    assert [r["id"] for r in ranked] == ["plain", "duet", "note"]


def test_resolve_reports_when_a_different_recording_had_the_data():
    track = {"artist": "A", "title": "S", "genre": "Rock"}
    client = FakeClient(
        {"S": [hit("first", "S", "A"), hit("second", "S", "A")]}, {"second": record()}
    )
    found = pilot.resolve_track(client, track)
    assert found["mbid"] == "second"
    assert found["needed_different_recording"] is True


def test_resolve_rejects_a_record_much_shorter_than_its_recording():
    track = {"artist": "A", "title": "S", "genre": "Rock"}
    client = FakeClient(
        {"S": [hit("short", "S", "A", length=400_000)]}, {"short": record(length=200.0)}
    )
    assert pilot.resolve_track(client, track) is None


@pytest.mark.parametrize(
    "tags",
    [
        {"artist": "Devo", "title": "S"},
        {"artist": "A", "title": "S (Live)"},
    ],
)
def test_resolve_rejects_a_record_tagged_as_something_else(tags):
    track = {"artist": "A", "title": "S", "genre": "Rock"}
    client = FakeClient({"S": [hit("wrong", "S", "A")]}, {"wrong": record(**tags)})
    assert pilot.resolve_track(client, track) is None


def test_resolve_accepts_a_tag_that_only_drops_the_leading_the():
    track = {"artist": "The Dave Brubeck Quartet", "title": "S", "genre": "Rock"}
    tagged = record(artist="Dave Brubeck Quartet")
    client = FakeClient(
        {"S": [hit("ok", "S", "The Dave Brubeck Quartet")]}, {"ok": tagged}
    )
    assert pilot.resolve_track(client, track)["mbid"] == "ok"


def test_resolve_skips_a_cut_far_from_the_songs_usual_length():
    track = {"artist": "A", "title": "S", "genre": "Rock"}
    recordings = [
        hit("long", "S", "A", length=300_000),
        hit("usual", "S", "A", length=200_000),
        hit("usual2", "S", "A", length=200_000),
    ]
    client = FakeClient(
        {"S": recordings},
        {"long": record(length=300.0), "usual": record(length=200.0)},
    )
    assert pilot.resolve_track(client, track)["mbid"] == "usual"


def test_resolve_skips_a_record_missing_a_feature():
    track = {"artist": "A", "title": "S", "genre": "Rock"}
    broken = record()
    del broken["lowlevel"]["spectral_rms"]
    client = FakeClient({"S": [hit("bad", "S", "A")]}, {"bad": broken})
    assert pilot.resolve_track(client, track) is None


def test_fetch_replaces_a_track_with_no_data_from_the_same_genres_alternate(tmp_path):
    plan = {
        "tracks": [{"artist": "Gone", "title": "Lost", "genre": "Pop"}],
        "alternates": [
            {"artist": "R", "title": "Wrong genre", "genre": "Rock"},
            {"artist": "P", "title": "Spare", "genre": "Pop"},
        ],
    }
    client = FakeClient(
        {"Spare": [hit("spare", "Spare", "P")]},
        {"spare": record(artist="P", title="Spare")},
    )
    manifest = pilot.fetch(client, plan, tmp_path)
    assert [t["title"] for t in manifest["tracks"]] == ["Spare"]
    assert manifest["replaced"][0]["track"]["title"] == "Lost"
    assert (tmp_path / "records" / "spare.json").exists()
    assert "record" not in manifest["tracks"][0]


def test_fetch_stops_when_a_genre_runs_out_of_alternates(tmp_path):
    plan = {
        "tracks": [{"artist": "G", "title": "Lost", "genre": "Pop"}],
        "alternates": [],
    }
    with pytest.raises(pilot.PilotError, match="no alternate left"):
        pilot.fetch(FakeClient({}, {}), plan, tmp_path)


def test_vector_lines_up_with_columns_and_turns_variance_into_spread():
    vector = pilot.vector_from_record(record(base=4.0))
    columns = pilot.feature_columns()
    assert vector.size == len(columns) == 38
    assert vector[columns.index("mfcc_mean_01")] == 4.0
    assert vector[columns.index("mfcc_std_01")] == 2.0
    assert vector[columns.index("zcr_median_01")] == pytest.approx(4.1)
    assert vector[columns.index("rmse_std_01")] == pytest.approx(4.2**0.5)


def test_vector_refuses_a_non_finite_value():
    bad = record()
    bad["lowlevel"]["spectral_rms"]["mean"] = float("nan")
    with pytest.raises(ValueError):
        pilot.vector_from_record(bad)


def test_standardized_handles_a_constant_column():
    z = pilot.standardized(np.array([[1.0, 5.0], [3.0, 5.0]]))
    assert z[:, 0].tolist() == [-1.0, 1.0]
    assert z[:, 1].tolist() == [0.0, 0.0]


def test_match_finds_the_row_at_the_same_standardized_place_across_scales():
    catalog_rows = np.array(
        [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]]
    )
    # Same shape on a different scale: each side is standardized against itself.
    known = catalog_rows[[0, 2, 4]] * 1000 + 7
    assert pilot.match(known, catalog_rows)[:, 0].tolist() == [0, 2, 4]


def build_key(seed: int = 7, count: int = 4) -> tuple[list[dict], pd.DataFrame]:
    frame = catalog()
    # Every known track wants the same neighbor, the case that would repeat a link.
    order = np.tile(np.arange(len(frame)), (count, 1))
    return pilot.build_pairs(known_tracks(count), frame, order, seed), frame


def test_no_track_appears_twice_even_when_tracks_share_a_nearest_neighbor():
    key, _ = build_key()
    shown = [e["bridge_track_id"] for e in key] + [e["control_track_id"] for e in key]
    assert len(set(shown)) == len(shown)
    assert sorted(e["bridge_neighbor_rank"] for e in key) == [0, 1, 2, 3]


def test_control_shares_the_picks_genre_even_when_the_pick_is_off_genre():
    frame = catalog()
    # Every track's nearest neighbors are Rock, so the Pop tracks' picks are off-genre.
    order = np.tile(np.arange(len(frame)), (4, 1))
    key = pilot.build_pairs(known_tracks(4), frame, order, 7)
    genres = dict(zip(frame["track_id"], frame["genre_top"], strict=True))
    assert {e["genre"] for e in key} == {"Rock", "Pop"}
    assert {genres[e["bridge_track_id"]] for e in key} == {"Rock"}
    assert all(
        genres[e["control_track_id"]]
        == genres[e["bridge_track_id"]]
        == e["bridge_genre"]
        for e in key
    )


def test_pairs_repeat_for_a_seed_and_change_with_it():
    first, _ = build_key(7)
    again, _ = build_key(7)
    other, _ = build_key(8)
    assert first == again
    assert first != other


def test_slots_and_order_do_not_follow_the_role_or_the_list():
    # Across seeds, the bridge lands in A about half the time and order is shuffled.
    slots, kept_order = [], 0
    for seed in range(200):
        key, _ = build_key(seed)
        slots += [e["bridge_slot"] for e in key]
        kept_order += [e["title"] for e in key] == [t["title"] for t in known_tracks()]
    assert 0.4 < slots.count("A") / len(slots) < 0.6
    assert kept_order < 30


def test_control_pool_running_out_is_an_error():
    frame = catalog(tracks_per_genre=2)
    order = np.tile(np.arange(len(frame)), (4, 1))
    with pytest.raises(pilot.PilotError, match="no control left"):
        pilot.build_pairs(known_tracks(4), frame, order, 1)


def test_listening_view_and_page_carry_no_role():
    key, frame = build_key()
    pairs = pilot.listening_pairs(key, frame)
    assert all(set(p) == {"pair", "artist", "title", "a", "b"} for p in pairs)
    urls = dict(zip(frame["track_id"], frame["source_url"], strict=True))
    for entry, pair in zip(key, pairs, strict=True):
        slot = pair["a"] if entry["bridge_slot"] == "A" else pair["b"]
        assert slot == urls[entry["bridge_track_id"]]
    page = pilot.render_page(pairs)
    assert "__PAIRS__" not in page
    embedded = page.split('type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded) == pairs
    lowered = page.lower()
    for leak in ("bridge_", "control", "neighbor", "bridge_slot", "seed", "track_id"):
        assert leak not in lowered


def test_links_show_nothing_but_the_tracks_own_source_url():
    key, frame = build_key()
    allowed = set(frame["source_url"])
    assert all({p["a"], p["b"]} <= allowed for p in pilot.listening_pairs(key, frame))


def write_prepared_inputs(data_dir: Path, count: int = 2) -> dict:
    out = pilot.pilot_dir(data_dir)
    (out / "records").mkdir(parents=True)
    tracks = []
    for i, track in enumerate(known_tracks(count)):
        tracks.append({**track, "mbid": f"m{i}"})
        (out / "records" / f"m{i}.json").write_text(
            json.dumps(record(i + 1.0)), encoding="utf-8"
        )
    (out / "resolved.json").write_text(json.dumps({"tracks": tracks}), encoding="utf-8")
    (data_dir / "catalog").mkdir()
    catalog().to_parquet(data_dir / "catalog" / "catalog.parquet")
    return {"pairs": count, "seed": 3, "pass_wins": 2}


def test_prepare_seals_the_key_and_prints_no_track_id(tmp_path, capsys):
    plan = write_prepared_inputs(tmp_path)
    summary = pilot.prepare(plan, tmp_path)
    printed = capsys.readouterr().out
    key = json.loads(pilot.key_path(tmp_path).read_text(encoding="utf-8"))["pairs"]
    assert summary["pairs"] == 2
    for entry in key:
        assert str(entry["bridge_track_id"]) not in printed
        assert str(entry["control_track_id"]) not in printed
    page = (pilot.pilot_dir(tmp_path) / "listen.html").read_text(encoding="utf-8")
    assert "bridge_slot" not in page


def test_prepare_refuses_to_reseal(tmp_path):
    plan = write_prepared_inputs(tmp_path)
    pilot.prepare(plan, tmp_path)
    with pytest.raises(pilot.PilotError, match="reseal"):
        pilot.prepare(plan, tmp_path)


def test_prepare_needs_fetch_and_every_pair(tmp_path):
    with pytest.raises(pilot.PilotError, match="run fetch first"):
        pilot.prepare({"pairs": 2}, tmp_path)
    write_prepared_inputs(tmp_path)
    with pytest.raises(pilot.PilotError, match="expected 30"):
        pilot.prepare({"pairs": 30, "seed": 1}, tmp_path)


def test_playable_rules_out_picks_and_controls_alike():
    frame = catalog(tracks_per_genre=16)
    order = np.tile(np.arange(len(frame)), (4, 1))
    dead = {100, 101, 108, 109, 110}
    tracks = known_tracks(4)
    key = pilot.build_pairs(tracks, frame, order, 7, lambda t: t not in dead)
    shown = {e["bridge_track_id"] for e in key} | {e["control_track_id"] for e in key}
    assert not shown & dead
    assert min(e["bridge_neighbor_rank"] for e in key) >= 2


def test_all_playable_gives_the_same_pairs_as_no_check():
    frame = catalog()
    order = np.tile(np.arange(len(frame)), (4, 1))
    plain = pilot.build_pairs(known_tracks(4), frame, order, 7)
    assert pilot.build_pairs(known_tracks(4), frame, order, 7, lambda _: True) == plain


def test_live_variant_keeps_its_own_key_and_page_and_leaves_the_first_alone(tmp_path):
    plan = write_prepared_inputs(tmp_path)
    pilot.prepare(plan, tmp_path)
    first = pilot.key_path(tmp_path).read_text(encoding="utf-8")

    class AllDeadButLate:
        def is_playable(self, url):
            return not url.endswith(("t0", "t1", "t2"))

        def counts(self):
            return {"links_checked": 9, "links_dead": 3}

    summary = pilot.prepare(plan, tmp_path, live_links=True, checker=AllDeadButLate())
    assert summary["links_dead"] == 3
    assert pilot.key_path(tmp_path).read_text(encoding="utf-8") == first
    assert pilot.key_path(tmp_path, live_links=True).exists()
    page = pilot.pilot_dir(tmp_path) / "listen-live.html"
    assert "freemusicarchive.org/music/t0" not in page.read_text(encoding="utf-8")


def test_sign_test_matches_the_bar_quoted_in_the_report():
    assert round(pilot.sign_test(21, 30), 3) == 0.043
    assert round(pilot.sign_test(20, 30), 3) == 0.099
    assert pilot.sign_test(15, 30) == 1.0
    assert pilot.sign_test(0, 0) == 1.0
    assert pilot.sign_test(9, 30) == pilot.sign_test(21, 30)


def key_of(n: int) -> list[dict]:
    return [
        {
            "pair": i + 1,
            "bridge_slot": "A",
            "bridge_neighbor_rank": 0,
            "bridge_genre": "Rock",
            "genre": "Rock",
        }
        for i in range(n)
    ]


def test_score_passes_at_exactly_the_bar_and_fails_below_it():
    key = key_of(30)
    at_bar = {str(i + 1): "A" if i < 21 else "B" for i in range(30)}
    below = {str(i + 1): "A" if i < 20 else "B" for i in range(30)}
    assert pilot.score(key, at_bar, PLAN)["passed"] is True
    assert pilot.score(key, below, PLAN)["passed"] is False


def test_unplayable_pairs_cannot_win_and_are_counted():
    key = key_of(30)
    picks = {str(i + 1): "A" if i < 20 else "unplayable" for i in range(30)}
    result = pilot.score(key, picks, PLAN)
    assert (result["wins"], result["unplayable"], result["passed"]) == (20, 10, False)
    assert result["sign_test_p_played_pairs"] < result["sign_test_p_all_pairs"]
    assert "can't prove a good one" in result["limit"]


def test_score_refuses_a_missing_pick():
    with pytest.raises(pilot.PilotError, match="pair 2 has no pick"):
        pilot.score(key_of(2), {"1": "A"}, PLAN)


def test_unblind_end_to_end_writes_the_result(tmp_path, capsys):
    plan = write_prepared_inputs(tmp_path)
    pilot.prepare(plan, tmp_path)
    capsys.readouterr()
    key = json.loads(pilot.key_path(tmp_path).read_text(encoding="utf-8"))["pairs"]
    picks = tmp_path / "picks.json"
    picks.write_text(
        json.dumps({str(e["pair"]): e["bridge_slot"] for e in key}), encoding="utf-8"
    )
    result = pilot.unblind(plan, tmp_path, picks)
    assert result["wins"] == 2 and result["passed"] is True
    assert (pilot.pilot_dir(tmp_path) / "result.json").exists()


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict, url: str = "u"):
        super().__init__(body)
        self.headers = headers
        self._url = url

    def geturl(self) -> str:
        return self._url


PAGE_URL = "https://freemusicarchive.org/music/a/b/c"


def player_page(url: str = PAGE_URL) -> bytes:
    info = json.dumps({"url": url, "fileUrl": "https://files.example/c.mp3"})
    return f"<div data-track-info='{info.replace(chr(34), '&quot;')}'>".encode()


def checker_for(tmp_path, answers: list) -> tuple[pilot.LinkChecker, list]:
    calls = []

    def opener(request, timeout):
        calls.append(request.get_method())
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return pilot.LinkChecker(tmp_path / "links.json", opener, lambda _: None), calls


def test_audio_url_ignores_a_trailing_slash_on_the_players_url():
    page = player_page(PAGE_URL + "/").decode()
    assert pilot._audio_url(page, PAGE_URL) == "https://files.example/c.mp3"


def test_link_checker_asks_for_audio_headers_only_and_remembers(tmp_path):
    checker, calls = checker_for(
        tmp_path,
        [
            FakeResponse(player_page(), {}, PAGE_URL),
            FakeResponse(b"", {"Content-Type": "audio/mpeg"}),
        ],
    )
    assert checker.is_playable("http://old") is True
    assert checker.is_playable("http://old") is True
    assert calls == ["GET", "HEAD"]
    assert checker.counts() == {"links_checked": 1, "links_dead": 0}
    reopened = pilot.LinkChecker(tmp_path / "links.json", None, lambda _: None)
    assert reopened.is_playable("http://old") is True


@pytest.mark.parametrize(
    "answers",
    [
        [None],
        [FakeResponse(b"<html>no player</html>", {}, PAGE_URL)],
        [FakeResponse(player_page("https://other/track"), {}, PAGE_URL)],
        [
            FakeResponse(player_page(), {}, PAGE_URL),
            FakeResponse(b"", {"Content-Type": "text/html"}),
        ],
    ],
)
def test_link_checker_counts_gone_pages_and_missing_audio_as_dead(tmp_path, answers):
    if answers == [None]:
        answers = [urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO())]
    checker, _ = checker_for(tmp_path, answers)
    assert checker.is_playable("http://old") is False
    assert checker.counts() == {"links_checked": 1, "links_dead": 1}


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO()),
        urllib.error.URLError("down"),
    ],
)
def test_link_checker_stops_when_it_cannot_tell(tmp_path, error):
    checker, _ = checker_for(tmp_path, [error])
    with pytest.raises(pilot.PilotError):
        checker.is_playable("http://old")


def http_error(code: int, headers: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", headers, io.BytesIO())


def test_client_waits_out_the_rate_limit_and_retries():
    slept: list[float] = []
    answers = [
        http_error(429, {"X-RateLimit-Reset-In": "9"}),
        FakeResponse(
            b'{"ok": 1}', {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "5"}
        ),
    ]

    def opener(request, timeout):
        assert "music-discovery" in request.get_header("User-agent")
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = pilot.Client(opener=opener, sleep=slept.append)
    assert client.get("http://x") == {"ok": 1}
    assert slept == [pilot.REQUEST_GAP_SECONDS, 10.0, pilot.REQUEST_GAP_SECONDS, 6.0]


def test_client_cache_answers_a_second_time_without_asking(tmp_path):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(b'{"ok": 1}', {})

    client = pilot.Client(opener, lambda _: None, cache_dir=tmp_path / "cache")
    assert client.get("http://x") == {"ok": 1}
    assert client.get("http://x") == {"ok": 1}
    assert calls == ["http://x"]


def test_client_treats_404_as_no_data_and_other_errors_as_failure():
    def opener_for(error):
        def opener(request, timeout):
            raise error

        return opener

    quiet = lambda _: None  # noqa: E731
    assert pilot.Client(opener_for(http_error(404, {})), quiet).get("http://x") is None
    with pytest.raises(pilot.PilotError, match="HTTP 400"):
        pilot.Client(opener_for(http_error(400, {})), quiet).get("http://x")
    with pytest.raises(pilot.PilotError, match="failed"):
        pilot.Client(opener_for(urllib.error.URLError("down")), quiet).get("http://x")


def test_main_reports_errors_in_one_line(tmp_path, capsys):
    assert pilot.main(["prepare", "--data-dir", str(tmp_path)]) == 1
    assert "run fetch first" in capsys.readouterr().err
    assert pilot.main(["unblind", "--data-dir", str(tmp_path)]) == 1
    assert "needs the picks" in capsys.readouterr().err


@pytest.fixture(scope="module")
def page_scenarios(tmp_path_factory):
    """The page's real script, run in node on a stubbed DOM and store."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("the page scenarios need node on PATH")
        pytest.skip("node is not on PATH")
    pairs = [
        {
            "pair": n,
            "artist": "A",
            "title": "S",
            "a": f"http://a/{n}",
            "b": f"http://b/{n}",
        }
        for n in (1, 2, 3)
    ]
    page = tmp_path_factory.mktemp("page") / "listen.html"
    page.write_text(pilot.render_page(pairs), encoding="utf-8")
    harness = ROOT / "tests" / "bridge_pilot_page.mjs"
    done = subprocess.run(
        [node, str(harness), str(page)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout), pilot.kit_id(pairs)


def test_page_keeps_a_pick_whose_write_failed_and_sends_it_on_reload(page_scenarios):
    result = page_scenarios[0]["failed_write_survives_reload"]
    assert result == {"warned": True, "local": "B", "stored": "B"}


def test_page_warns_until_every_failed_write_has_landed(page_scenarios):
    result = page_scenarios[0]["warning_stays_until_every_failed_write_lands"]
    assert result == {"whileDown": True, "afterRetry": False, "stored": "B"}


def test_page_takes_a_newer_pick_from_another_device(page_scenarios):
    result = page_scenarios[0]["newer_pick_from_another_device_wins"]
    assert result == {"phone": "B", "stored": "B"}


def test_page_does_not_overwrite_a_newer_stored_pick(page_scenarios):
    result = page_scenarios[0]["older_write_does_not_overwrite_a_newer_stored_pick"]
    assert result == {"local": "B", "stored": "B"}


def test_page_writes_one_at_a_time_so_the_last_pick_lands_last(page_scenarios):
    assert page_scenarios[0]["last_of_two_quick_picks_lands"] == {"stored": "B"}


def test_page_keeps_picks_in_a_collection_of_its_own_kit(page_scenarios):
    results, kit = page_scenarios
    assert results["picks_live_in_a_collection_of_this_kit"] == {
        "collections": [f"picks-{kit}"]
    }


def test_kits_with_different_pairs_have_different_ids():
    pair = {"pair": 1, "artist": "A", "title": "S", "a": "http://a", "b": "http://b"}
    assert pilot.kit_id([pair]) != pilot.kit_id([{**pair, "a": "http://c"}])
