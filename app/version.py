"""Which version of the app is running, so an updated copy never talks to an old server.

The build id is a hash of the code, the screen files, the config, and the prompt. The server
remembers the id it started with; if the files on disk change after that (an update), it reports
itself as out of date: the launcher then restarts it, and an open page shows a notice.
"""
from __future__ import annotations

import hashlib
import time

from app.config import ROOT

PARTS = [("app", "*.py"), ("static", "*.js"), ("static", "*.css"), ("static", "*.html"), ("static", "*.png"),
         ("static/fonts", "*"), ("app/assets", "**/*"), ("config", "*.yaml"),
         ("config", "extraction.schema.json"), ("prompts", "extract_system.md")]


def fingerprint() -> str:
    h = hashlib.sha256()
    for folder, pattern in PARTS:
        for path in sorted(p for p in (ROOT / folder).glob(pattern) if p.is_file()):
            h.update(path.relative_to(ROOT).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:16]


RUNNING = fingerprint()
_disk = (0.0, RUNNING)


def on_disk() -> str:
    """The files' build id now (re-hashed at most every 2 seconds)."""
    global _disk
    if time.monotonic() - _disk[0] > 2:
        _disk = (time.monotonic(), fingerprint())
    return _disk[1]
