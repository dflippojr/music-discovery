"""Write a demo bundle from a synthetic catalog, for the browser and Node tests.

Usage: python scripts/build_demo_fixture.py <out_dir>

The catalog is made up (see tests/test_evaluation.py); no dataset is read.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_evaluation import make_frame  # noqa: E402

from musicdiscovery.catalog import Catalog  # noqa: E402
from musicdiscovery.demo import export_demo  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    frame = make_frame(per_genre=150)
    frame["title"] = [f"Synthetic track {t}" for t in frame["track_id"]]
    frame["license_url"] = "https://creativecommons.org/licenses/by/4.0/"
    frame["source_url"] = [f"https://example.com/track/{t}" for t in frame["track_id"]]
    size = export_demo(Catalog(frame), Path(argv[1]))
    print(f"wrote {argv[1]}: {size.raw} bytes, {size.compressed} bytes gzip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
