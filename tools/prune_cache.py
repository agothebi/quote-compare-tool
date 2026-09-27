"""Remove saved extractions from older prompt or config versions (never used again: the cache key
includes the versions). Entries that a saved comparison still points to are kept.

    python -m tools.prune_cache            # show what would be removed
    python -m tools.prune_cache --delete   # remove it
"""
from __future__ import annotations

import json
import sys

from app import storage
from app.config import ROOT, lines_config, settings


def stale_entries() -> list:
    cache = ROOT / settings()["cache_dir"]
    in_use = {f.get("cache_key") for c in storage._all_raw() for f in c.get("files", [])}
    current = (str(settings()["prompt_version"]), str(lines_config()["config_version"]))
    out = []
    for path in sorted(cache.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
        except (OSError, ValueError):
            out.append(path)  # unreadable: never usable
            continue
        if path.stem not in in_use and (str(meta.get("prompt_version")), str(meta.get("config_version"))) != current:
            out.append(path)
    return out


def main(argv: list[str]) -> int:
    paths = stale_entries()
    size = sum(p.stat().st_size for p in paths)
    print(f"{len(paths)} saved extractions from older versions ({size / 1e6:.1f} MB)")
    if "--delete" in argv:
        for p in paths:
            p.unlink(missing_ok=True)
        print("removed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
