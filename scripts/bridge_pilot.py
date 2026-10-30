"""Blind pilot of the sound bridge from known tracks to FMA (#227, #226).

Usage: python scripts/bridge_pilot.py fetch [--data-dir data]
       python scripts/bridge_pilot.py prepare [--data-dir data]
       python scripts/bridge_pilot.py unblind PICKS_JSON [--data-dir data]

`fetch` looks up the tracks in `eval/bridge_pilot.json` on MusicBrainz and saves
their AcousticBrainz low-level records. `prepare` matches them to the catalog
and writes the listening page and the sealed key. `unblind` scores the picks.

`prepare` prints counts only, and the page holds no role for any link: run
nothing that prints `sealed/key.json` until the listener has finished. All of
this stays under the ignored data directory. See docs/bridge-pilot.md.
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "eval" / "bridge_pilot.json"
PAGE_TEMPLATE = Path(__file__).resolve().parent / "bridge_pilot_page.html"

USER_AGENT = (
    "music-discovery-bridge-pilot/1 (https://github.com/dflippojr/music-discovery)"
)
MUSICBRAINZ = "https://musicbrainz.org/ws/2/recording"
ACOUSTICBRAINZ = "https://acousticbrainz.org/api/v1"
REQUEST_GAP_SECONDS = 1.1  # MusicBrainz allows about one request per second
ATTEMPTS = 4
SEARCH_LIMIT = 50
COUNT_BATCH = 25  # AcousticBrainz takes at most 25 ids per bulk call
MAX_RECORDS_TRIED = 5
LENGTH_TOLERANCE_SECONDS = 15.0

# Words that mark a recording as something other than the known studio version.
_NOT_THE_ORIGINAL = re.compile(
    r"\b(live|remix|mix|edit|karaoke|cover|demo|instrumental|acoustic|tribute|"
    r"medley|reprise|session|rehearsal|sped|slowed)\b"
)

STATISTICS = ("mean", "median", "std")
SCALAR_FEATURES = {
    "spectral_centroid": "spectral_centroid",
    "spectral_rolloff": "spectral_rolloff",
    "rmse": "spectral_rms",
    "zcr": "zerocrossingrate",
}
MFCC_COEFFICIENTS = 13
LIMIT_NOTE = (
    "A small sample from one listener can stop a bad idea but can't prove a good one."
)


class PilotError(Exception):
    """A step of the pilot can't go on; the message says why."""


def load_plan(path: Path = PLAN) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pilot_dir(data_dir: Path) -> Path:
    return data_dir / "bridge-pilot"


def key_path(data_dir: Path) -> Path:
    return pilot_dir(data_dir) / "sealed" / "key.json"


def _normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


class Client:
    """Fetch JSON with a gap between requests, honoring AcousticBrainz's limit."""

    def __init__(self, opener=urllib.request.urlopen, sleep=time.sleep):
        self._opener = opener
        self._sleep = sleep

    def get(self, url: str):
        """Return the decoded body, or None for a 404."""
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        for attempt in range(ATTEMPTS):
            self._sleep(REQUEST_GAP_SECONDS)
            try:
                with self._opener(request, timeout=30) as response:
                    self._respect_limit(response.headers)
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                if (
                    error.code not in (429, 500, 502, 503, 504)
                    or attempt == ATTEMPTS - 1
                ):
                    raise PilotError(f"{url} answered HTTP {error.code}") from error
                self._sleep(_reset_seconds(error.headers, 2**attempt))
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == ATTEMPTS - 1:
                    raise PilotError(f"{url} failed: {error}") from error
                self._sleep(2**attempt)
        raise PilotError(f"{url} failed")  # pragma: no cover

    def _respect_limit(self, headers) -> None:
        if headers.get("X-RateLimit-Remaining") == "0":
            self._sleep(_reset_seconds(headers, 1))


def _reset_seconds(headers, default: float) -> float:
    for name in ("X-RateLimit-Reset-In", "Retry-After"):
        value = headers.get(name) if headers else None
        if value and value.isdigit():
            return float(value) + 1
    return default


def _credit(recording: dict) -> str:
    return "".join(
        credit.get("name", "") + credit.get("joinphrase", "")
        for credit in recording.get("artist-credit", [])
    )


def rank_candidates(track: dict, recordings: list[dict]) -> list[dict]:
    """Keep recordings of this song by this artist, plainest first.

    Keeps the search's order otherwise, so the choice never depends on features.
    """
    title = _normalized(track["title"])
    artist = _normalized(track["artist"])
    kept = []
    for recording in recordings:
        credit = _normalized(_credit(recording))
        if (
            _normalized(recording.get("title", "")) != title
            or f" {artist} " not in f" {credit} "
        ):
            continue
        comment = _normalized(recording.get("disambiguation", ""))
        if _NOT_THE_ORIGINAL.search(comment):
            continue
        kept.append((bool(comment), credit != artist, len(kept), recording))
    return [entry[-1] for entry in sorted(kept, key=lambda entry: entry[:3])]


def _search(client: Client, track: dict) -> list[dict]:
    query = (
        f'recording:"{track["title"]}" AND artist:"{track["artist"]}" AND video:false'
    )
    params = urllib.parse.urlencode(
        {"query": query, "limit": SEARCH_LIMIT, "fmt": "json"}
    )
    return (client.get(f"{MUSICBRAINZ}?{params}") or {}).get("recordings", [])


def _with_data(client: Client, ids: list[str]) -> set[str]:
    found = set()
    for start in range(0, len(ids), COUNT_BATCH):
        batch = ";".join(ids[start : start + COUNT_BATCH])
        answer = client.get(f"{ACOUSTICBRAINZ}/count?recording_ids={batch}") or {}
        found |= {mbid for mbid, counts in answer.items() if counts}
    return found


def _usable(record: dict | None, length_ms: int | None) -> bool:
    """A record the matcher can read, about as long as the recording it claims."""
    if not record:
        return False
    try:
        vector_from_record(record)
        seconds = float(record["metadata"]["audio_properties"]["length"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return not length_ms or abs(seconds - length_ms / 1000) <= LENGTH_TOLERANCE_SECONDS


def resolve_track(client: Client, track: dict) -> dict | None:
    """Find the first usable record of this song, or None when it has none."""
    candidates = rank_candidates(track, _search(client, track))
    ids = [candidate["id"] for candidate in candidates]
    available = _with_data(client, ids)
    tried = 0
    for position, candidate in enumerate(candidates):
        if candidate["id"] not in available or tried == MAX_RECORDS_TRIED:
            continue
        tried += 1
        record = client.get(f"{ACOUSTICBRAINZ}/{candidate['id']}/low-level")
        if _usable(record, candidate.get("length")):
            return {
                **track,
                "mbid": candidate["id"],
                "candidates": len(candidates),
                "needed_different_recording": position > 0,
                "record": record,
            }
    return None


def fetch(client: Client, plan: dict, out_dir: Path) -> dict:
    """Save a record per track; a track with none gets its genre's next alternate."""
    records_dir = out_dir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    alternates = list(plan["alternates"])
    chosen, replaced = [], []
    for track in plan["tracks"]:
        found = resolve_track(client, track)
        while found is None:
            spare = next((a for a in alternates if a["genre"] == track["genre"]), None)
            if spare is None:
                raise PilotError(
                    f"no record for {track['title']} and no alternate left"
                )
            alternates.remove(spare)
            found = resolve_track(client, spare)
            replaced.append(
                {"track": track, "tried": spare, "found": found is not None}
            )
        record = found.pop("record")
        (records_dir / f"{found['mbid']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        chosen.append(found)
        print(
            f"{found['artist']} - {found['title']}: {found['mbid']}"
            f"{' (different recording)' if found['needed_different_recording'] else ''}"
        )
    manifest = {"tracks": chosen, "replaced": replaced}
    (out_dir / "resolved.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def feature_columns() -> list[str]:
    """Catalog columns the bridge compares, in the order of `vector_from_record`."""
    columns = [
        f"mfcc_{stat}_{index:02d}"
        for stat in ("mean", "std")
        for index in range(1, MFCC_COEFFICIENTS + 1)
    ]
    for name in SCALAR_FEATURES:
        columns += [f"{name}_{stat}_01" for stat in STATISTICS]
    return columns


def vector_from_record(record: dict) -> np.ndarray:
    """Read an AcousticBrainz record's values for `feature_columns`.

    Essentia gives a variance where librosa's table has a standard deviation, so
    the square root makes both a spread. Raises on a missing or non-finite value.
    """
    low = record["lowlevel"]
    mfcc = low["mfcc"]
    values = list(mfcc["mean"][:MFCC_COEFFICIENTS])
    values += [math.sqrt(mfcc["cov"][i][i]) for i in range(MFCC_COEFFICIENTS)]
    for name in SCALAR_FEATURES.values():
        values += [low[name]["mean"], low[name]["median"], math.sqrt(low[name]["var"])]
    vector = np.asarray(values, dtype=np.float64)
    if vector.size != len(feature_columns()) or not np.isfinite(vector).all():
        raise ValueError("record has missing or non-finite features")
    return vector


def standardized(matrix: np.ndarray) -> np.ndarray:
    """Z-score each column; a constant column becomes zeros."""
    spread = matrix.std(axis=0)
    spread[spread == 0] = 1.0
    return (matrix - matrix.mean(axis=0)) / spread


def match(known: np.ndarray, catalog: np.ndarray) -> np.ndarray:
    """For each known row, catalog positions from nearest to farthest.

    Each side is standardized against itself, because the two extractors' values
    can't be compared raw. Ties break on position, so the result is deterministic.
    """
    known_z = standardized(known)
    catalog_z = standardized(catalog)
    return np.stack(
        [
            np.argsort(((catalog_z - row) ** 2).sum(axis=1), kind="stable")
            for row in known_z
        ]
    )


def build_pairs(
    tracks: list[dict], frame: pd.DataFrame, order: np.ndarray, seed: int
) -> list[dict]:
    """Pair each track's bridge pick with a random track of the track's genre.

    The genre is the known track's own: the control stands for a genre stand-in,
    which is what the bridge has to beat. A pick already used falls to the next
    nearest track, and no track appears twice, because a link seen in two pairs
    would give away the bridge's. Returns the full key, in listening order.
    """
    rng = np.random.default_rng(seed)
    ids = frame["track_id"].to_numpy()
    genres = dict(zip(frame["track_id"].astype(int), frame["genre_top"], strict=True))
    used: set[int] = set()
    picks = []
    for row in order:
        rank = next(
            rank for rank, position in enumerate(row) if int(ids[position]) not in used
        )
        pick = int(ids[row[rank]])
        used.add(pick)
        picks.append((pick, rank))
    entries = []
    for track, (pick, rank) in zip(tracks, picks, strict=True):
        pool = frame.loc[frame["genre_top"] == track["genre"], "track_id"].to_numpy(
            dtype=int
        )
        pool = np.sort(pool[~np.isin(pool, list(used))])
        if not len(pool):
            raise PilotError(f"no control left in genre {track['genre']}")
        control = int(rng.choice(pool))
        used.add(control)
        entries.append(
            {
                "artist": track["artist"],
                "title": track["title"],
                "genre": track["genre"],
                "mbid": track.get("mbid"),
                "bridge_track_id": pick,
                "control_track_id": control,
                "bridge_neighbor_rank": rank,
                "bridge_genre": genres[pick],
                "bridge_slot": "A" if rng.random() < 0.5 else "B",
            }
        )
    listening_order = rng.permutation(len(entries))
    return [{"pair": n + 1, **entries[i]} for n, i in enumerate(listening_order)]


def listening_pairs(key: list[dict], frame: pd.DataFrame) -> list[dict]:
    """The only view of the pairs the listener gets: a song and two links."""
    urls = dict(zip(frame["track_id"].astype(int), frame["source_url"], strict=True))
    pairs = []
    for entry in key:
        bridge = urls[entry["bridge_track_id"]]
        control = urls[entry["control_track_id"]]
        a, b = (bridge, control) if entry["bridge_slot"] == "A" else (control, bridge)
        pairs.append(
            {
                "pair": entry["pair"],
                "artist": entry["artist"],
                "title": entry["title"],
                "a": a,
                "b": b,
            }
        )
    return pairs


def render_page(pairs: list[dict], template: Path = PAGE_TEMPLATE) -> str:
    payload = json.dumps(pairs, ensure_ascii=True).replace("</", "<\\/")
    return template.read_text(encoding="utf-8").replace("__PAIRS__", payload)


def prepare(plan: dict, data_dir: Path) -> dict:
    """Match, pair and seal. Prints nothing that tells a pick from a control."""
    out_dir = pilot_dir(data_dir)
    resolved_path = out_dir / "resolved.json"
    if not resolved_path.exists():
        raise PilotError(f"{resolved_path} is missing; run fetch first")
    sealed = key_path(data_dir)
    if sealed.exists():
        raise PilotError(f"{sealed} exists; a second prepare would reseal the pilot")
    tracks = json.loads(resolved_path.read_text(encoding="utf-8"))["tracks"]
    if len(tracks) != plan["pairs"]:
        raise PilotError(f"expected {plan['pairs']} tracks, resolved {len(tracks)}")
    frame = pd.read_parquet(
        data_dir / "catalog" / "catalog.parquet",
        columns=["track_id", "genre_top", "source_url", *feature_columns()],
    )
    frame = frame[frame["source_url"].fillna("").str.startswith("http")].reset_index(
        drop=True
    )
    known = np.stack(
        [
            vector_from_record(
                json.loads(
                    (out_dir / "records" / f"{t['mbid']}.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            for t in tracks
        ]
    )
    order = match(known, frame[feature_columns()].to_numpy(dtype=np.float64))
    key = build_pairs(tracks, frame, order, plan["seed"])
    sealed.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"seed": plan["seed"], "pairs": key}, indent=2)
    sealed.write_text(body, encoding="utf-8")
    page = out_dir / "listen.html"
    page.write_text(render_page(listening_pairs(key, frame)), encoding="utf-8")
    summary = {
        "pairs": len(key),
        "catalog_rows": len(frame),
        "page": str(page),
        "key_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    print(json.dumps(summary, indent=2))
    return summary


def sign_test(wins: int, n: int) -> float:
    """Two-sided exact sign test of `wins` out of `n` against even odds."""
    if n == 0:
        return 1.0
    extreme = max(wins, n - wins)
    tail = sum(math.comb(n, k) for k in range(extreme, n + 1))
    return min(1.0, 2 * tail / 2**n)


def score(key: list[dict], picks: dict[str, str], plan: dict) -> dict:
    """Score picks ("A", "B" or "unplayable", by pair number) against the key.

    The bar counts wins out of all pairs, so an unplayable or missing pair can
    only fail to win. The test is reported on all pairs and on the played ones.
    """
    wins, losses, unplayable = 0, 0, 0
    rows = []
    for entry in key:
        pick = picks.get(str(entry["pair"]))
        if pick == entry["bridge_slot"]:
            outcome, wins = "win", wins + 1
        elif pick in ("A", "B"):
            outcome, losses = "loss", losses + 1
        elif pick == "unplayable":
            outcome, unplayable = "unplayable", unplayable + 1
        else:
            raise PilotError(f"pair {entry['pair']} has no pick")
        rows.append({**entry, "pick": pick, "outcome": outcome})
    played = wins + losses
    return {
        "wins": wins,
        "pairs": len(key),
        "losses": losses,
        "unplayable": unplayable,
        "bar": plan["pass_wins"],
        "passed": wins >= plan["pass_wins"],
        "sign_test_p_all_pairs": sign_test(wins, len(key)),
        "sign_test_p_played_pairs": sign_test(wins, played),
        "bumped_picks": sum(1 for entry in key if entry["bridge_neighbor_rank"] > 0),
        "off_genre_picks": sum(
            1 for entry in key if entry["bridge_genre"] != entry["genre"]
        ),
        "limit": LIMIT_NOTE,
        "rows": rows,
    }


def unblind(plan: dict, data_dir: Path, picks_path: Path) -> dict:
    """Score the finished picks and write the result next to the key."""
    picks = json.loads(picks_path.read_text(encoding="utf-8"))
    key = json.loads(key_path(data_dir).read_text(encoding="utf-8"))["pairs"]
    result = score(key, picks, plan)
    out = pilot_dir(data_dir) / "result.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {name: value for name, value in result.items() if name != "rows"}, indent=2
        )
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("fetch", "prepare", "unblind"))
    parser.add_argument("picks", nargs="?", type=Path, help="picks JSON for unblind")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    plan = load_plan()
    try:
        if args.command == "fetch":
            fetch(Client(), plan, pilot_dir(args.data_dir))
        elif args.command == "prepare":
            prepare(plan, args.data_dir)
        else:
            if args.picks is None:
                raise PilotError("unblind needs the picks JSON")
            unblind(plan, args.data_dir, args.picks)
    except (PilotError, OSError) as error:
        print(f"bridge_pilot: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
