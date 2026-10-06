"""Refresh the checked-in ALPR/Flock camera snapshot (warmap/data/
flock_cameras.json) that the map overlay draws.

Pulls the current set from DeFlock's bulk export (falling back to Overpass),
normalizes it, and writes the snapshot atomically, using the same posture as
tools/fetch_ids.py. On any failure the existing snapshot is left untouched and
the script exits non-zero with the reason. All the real work lives in
warmap/alpr_fetch.py so the app's `warmap flock --refresh` shares this code.

    python3 tools/fetch_flock.py                    # -> warmap/data/flock_cameras.json
    python3 tools/fetch_flock.py --output /tmp/f.json
    python3 tools/fetch_flock.py --prefer overpass  # worldwide, if it answers
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from warmap import alpr_fetch  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "warmap" / "data" / "flock_cameras.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"where to write the snapshot (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--prefer", choices=("deflock", "overpass"), default="deflock",
                        help="which source to try first (default: deflock)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite even if the new set is much smaller than the current one")
    args = parser.parse_args(argv)

    try:
        result = alpr_fetch.fetch_cameras(prefer=args.prefer)
        generated = datetime.now(timezone.utc).date().isoformat()
        size = alpr_fetch.write_snapshot(result, args.output, generated=generated,
                                         force=args.force)
    except alpr_fetch.FetchError as exc:
        print(f"fetch-flock: {exc}", file=sys.stderr)
        print("fetch-flock: leaving the existing snapshot untouched.", file=sys.stderr)
        return 1

    print(f"{args.output}: {result.count} cameras, {size} bytes "
          f"(source: {result.source_label})")
    if result.dropped:
        print(f"  dropped {result.dropped} mis-tagged (highway ways, not cameras)")
    for warning in result.errors:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
