"""Write a synthetic catalog whose titles and artists a legacy console cannot encode.

Usage: python scripts/build_console_catalog.py <data_dir>

CI then runs ``musicdiscovery recommend --data-dir <data_dir>`` on Windows with
the runner's default console encoding, so a crash like #28 fails the run. The
catalog is made up (see tests/test_evaluation.py); no dataset is read.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_evaluation import make_frame  # noqa: E402

# CJK, Cyrillic, Greek and an emoji are outside cp1252; Ångström is inside it.
TITLES = ["東京の夜", "Кино", "Ångström", "Ωμέγα", "🎵 Song"]
ARTISTS = ["Мумий Тролль", "宇多田", "Björk"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    frame = make_frame()
    ids = frame["track_id"]
    frame["title"] = [f"{TITLES[t % len(TITLES)]} {t}" for t in ids]
    frame["artist"] = [f"{ARTISTS[t % len(ARTISTS)]} {t % 25}" for t in ids]
    out = Path(argv[1]) / "catalog"
    out.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out / "catalog.parquet", index=False)
    print(f"wrote {out / 'catalog.parquet'}: {len(frame)} tracks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
