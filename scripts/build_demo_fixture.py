"""Write a demo bundle from a synthetic catalog, for the browser and Node tests.

Usage: python scripts/build_demo_fixture.py [--no-shape] <out_dir>

The catalog is made up (see tests/test_evaluation.py); no dataset is read.
A second catalog shaped like the real one (tests/catalog_factory.py) is
exported to <out_dir>/shape and its exclusion cases go in exclusion.json, so the
parity test also runs on missing genres, sparse tags and non-Latin titles.
--no-shape skips it and keeps <out_dir> flat (the disposable UI instance).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_evaluation import make_frame  # noqa: E402

from musicdiscovery.catalog import Catalog  # noqa: E402
from musicdiscovery.demo import build_catalog, export_demo, parity_queries  # noqa: E402
from musicdiscovery.evaluation import make_ranker  # noqa: E402
from musicdiscovery.preference import Preference  # noqa: E402


def exclusion_cases(catalog: Catalog) -> dict:
    """Explicit false/true queries against Python, separate from default parity."""
    rankers = {name: make_ranker(name) for name in ("baseline", "hybrid")}
    queries = []
    for query in parity_queries(catalog):
        for exclude in (False, True):
            preference = Preference.from_strings(
                [query["seed"]],
                query["likes"],
                query["dislikes"],
                exclude_seed_artist=exclude,
            )
            queries.append(
                {
                    **query,
                    "excludeSeedArtist": exclude,
                    "expected": {
                        name: [r.track_id for r in ranker.rank(preference, catalog)]
                        for name, ranker in rankers.items()
                    },
                }
            )
    return {"catalog": build_catalog(catalog), "queries": queries}


SHAPE_TRACKS = 400


def main(argv: list[str]) -> int:
    shape = argv[1:2] != ["--no-shape"]
    if not shape:
        argv = [argv[0], *argv[2:]]
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    frame = make_frame(per_genre=150)
    frame["title"] = [f"Synthetic track {t}" for t in frame["track_id"]]
    frame["license_url"] = "https://creativecommons.org/licenses/by/4.0/"
    frame["source_url"] = [f"https://example.com/track/{t}" for t in frame["track_id"]]
    size = export_demo(Catalog(frame), Path(argv[1]))
    cases = {"repeated": exclusion_cases(Catalog(frame))}
    frame["artist"] = "Only artist"
    cases["one_artist"] = exclusion_cases(Catalog(frame))
    if shape:
        from catalog_factory import from_shape, load_shape

        shaped = Catalog(from_shape(load_shape(), SHAPE_TRACKS))
        shape_size = export_demo(shaped, Path(argv[1]) / "shape")
        cases["shape"] = exclusion_cases(shaped)
    (Path(argv[1]) / "exclusion.json").write_text(json.dumps(cases), encoding="utf-8")
    print(f"wrote {argv[1]}: {size.raw} bytes, {size.compressed} bytes gzip")
    if shape:
        print(
            f"wrote {argv[1]}/shape: {shape_size.raw} bytes from {SHAPE_TRACKS} tracks"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
