"""One folder per comparison, no database.

    data/comparisons/<id>/
      comparison.json          the saved state (see BUILD_PLAN.md, "comparison.json")
      files/<fid>.<ext>        the original upload
      pages/<fid>_p<n>.png     rendered pages for the evidence panel
      work/<fid>.pages.json    read + redacted page text (local only; never sent anywhere)
      work/<fid>.extraction.json   the extraction record (raw LLM output + meta)

Every change goes through `update()`: it takes the comparison's lock, loads the current file,
applies the change, bumps `revision`, and saves atomically (temp file + fsync + rename), so a
crash can never leave half a JSON file and two writers can never interleave.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import ROOT, settings

log = logging.getLogger("quote_compare")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
FILE_ID_RE = re.compile(r"^f[0-9a-f]{8}$")
ACTIVE = ("queued", "reading", "redacting", "extracting", "checking")
STAGES = ("working", "ready", "sent", "closed")  # the board's columns
OUTCOMES = ("bound", "lost")                     # set on a closed comparison
EXPORT_DEFAULTS = {"length": "full", "total": True, "gloss": True, "gtk": True}  # what the client PDF includes

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class NotFound(KeyError):
    pass


class Damaged(ValueError):
    """A comparison.json that can't be read (cut short by a sync tool, a full disk, a hand edit)."""


class Conflict(RuntimeError):
    """The change was based on a state that has changed since (e.g. notes edited in another window)."""

    def __init__(self, message: str, current=None):
        super().__init__(message)
        self.current = current


def root() -> Path:
    return ROOT / settings()["app"]["comparisons_dir"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lock(cid: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(cid, threading.RLock())


def check_id(cid: str) -> str:
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise NotFound(cid)
    return cid


def folder(cid: str) -> Path:
    return root() / check_id(cid)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):  # Windows: a virus scanner or indexer can hold the file for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, obj) -> None:
    atomic_write(path, json.dumps(obj, indent=1, ensure_ascii=False).encode("utf-8"))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40].strip("-") or "client"


# ---------------------------------------------------------------- comparisons

def create(client_name: str, address: str = "", other_names: list[str] | None = None,
           property_address: str = "") -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    base = f"{today}_{slug(client_name)}"
    root().mkdir(parents=True, exist_ok=True)
    with _locks_guard:  # choosing a free id and claiming its folder must not race
        cid, n = base, 1
        while (root() / cid).exists():
            n += 1
            cid = f"{base}-{n}"
        (root() / cid).mkdir()
    comp = normalize({
        "id": cid, "revision": 1, "created_at": now(), "updated_at": now(),
        "client": {"name": client_name.strip(), "address": (address or "").strip(),
                   "property_address": (property_address or "").strip(),
                   "other_names": [n.strip() for n in (other_names or []) if n.strip()]},
        "files": [], "quotes": [], "edits": [], "kept": {}, "recommendation": {}, "notes": "",
    })
    write_json(root() / cid / "comparison.json", comp)
    place(cid, "working", 0)
    return comp


def normalize(comp: dict) -> dict:
    """Fields added after a comparison was first saved get their defaults, so an older
    comparison.json loads as if it had always had them (it is saved that way on its next change).

    stage / outcome   the board column, and bound / lost once closed
    archived_at       when the broker archived it (off the board, files kept), or None
    current           {line: quote id} the client's current policy, compared against
    export            what the client PDF includes (EXPORT_DEFAULTS)
    fact_choices      {line: {fact id: on}} the broker's ticks on the drafted reasons
    own_reasons       {line: [{"text", "on"}]} reasons the broker typed
    The old preset reason keys ("reasons": {line: [key]}) become reasons of the broker's own.
    """
    if not isinstance(comp.get("client"), dict):
        comp["client"] = {}
    if not isinstance(comp["client"].get("name"), str):
        comp["client"]["name"] = ""
    if not isinstance(comp["client"].get("property_address"), str):  # the insured property, when not the client's address
        comp["client"]["property_address"] = ""
    for key in ("created_at", "updated_at"):
        if not isinstance(comp.get(key), str):
            comp[key] = ""
    for key, default in (("files", []), ("quotes", []), ("edits", []), ("kept", {}), ("recommendation", {}),
                         ("notes", ""), ("current", {}), ("fact_choices", {}), ("own_reasons", {})):
        if not isinstance(comp.get(key), type(default)):
            comp[key] = default
    if comp.get("stage") not in STAGES:
        comp["stage"] = "working"
    if comp.get("outcome") not in OUTCOMES or comp["stage"] != "closed":
        comp["outcome"] = None
    if not (isinstance(comp.get("archived_at"), str) and comp["archived_at"]):
        comp["archived_at"] = None
    export = comp.get("export") if isinstance(comp.get("export"), dict) else {}
    comp["export"] = {k: export.get(k, v) if type(export.get(k, v)) is type(v) else v for k, v in EXPORT_DEFAULTS.items()}
    if comp["export"]["length"] not in ("full", "short"):
        comp["export"]["length"] = "full"
    old = comp.pop("reasons", None)
    if isinstance(old, dict):
        labels = settings().get("recommendation_reasons") or {}
        for line, keys in old.items():
            if isinstance(keys, list) and line not in comp["own_reasons"]:
                comp["own_reasons"][line] = [{"text": labels[k], "on": True} for k in keys if k in labels]
    return comp


def load(cid: str) -> dict:
    path = folder(cid) / "comparison.json"
    if not path.exists():
        raise NotFound(cid)
    try:
        comp = read_json(path)
        if not isinstance(comp, dict):
            raise ValueError("not an object")
        comp = normalize(comp)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise Damaged(cid) from e
    comp["id"] = cid
    return comp


def update(cid: str, change: Callable[[dict], object]) -> dict:
    """Apply `change(comp)` under the comparison's lock and save. `change` edits comp in place;
    it may raise (nothing is saved). Returns the saved comparison."""
    with lock(cid):
        comp = load(cid)
        change(comp)
        comp["revision"] = comp.get("revision", 0) + 1
        comp["updated_at"] = now()
        write_json(folder(cid) / "comparison.json", comp)
        return comp


def list_raw() -> list[dict]:
    """Every readable comparison (archived ones too), in board order: by column, then the broker's
    order in the column. Comparisons the order file does not know (older ones) follow, newest change first."""
    comps = []
    if not root().exists():
        return comps
    for d in root().iterdir():
        path = d / "comparison.json"
        if not (d.is_dir() and ID_RE.match(d.name) and path.exists()):
            continue
        try:
            comps.append(load(d.name))
        except Damaged:
            # shown on the board (so the broker sees it and can delete it), never fatal
            log.warning("comparison %s: comparison.json is damaged", d.name)
            comps.append(normalize({"id": d.name, "client": {"name": f"Damaged file ({d.name})"}, "damaged": True}))
        except (OSError, NotFound):
            continue
    order = board_order()
    pos = {cid: i for ids in order.values() for i, cid in enumerate(ids)}
    comps.sort(key=lambda c: c["updated_at"], reverse=True)
    comps.sort(key=lambda c: (STAGES.index(c["stage"]), c["id"] not in order.get(c["stage"], ()),
                              pos.get(c["id"], 0) if c["id"] in order.get(c["stage"], ()) else 0))
    return comps


def list_all() -> list[dict]:
    return [{"id": comp["id"], "client_name": comp["client"]["name"], "created_at": comp["created_at"],
             "updated_at": comp["updated_at"], "quotes": len(comp.get("quotes", [])),
             "files": len(comp.get("files", [])), "stage": comp["stage"], "outcome": comp["outcome"],
             "processing": sum(1 for f in comp.get("files", []) if f["status"] in ACTIVE)} for comp in list_raw()]


# ---------------------------------------------------------------- board order

_order_lock = threading.Lock()


def _order_path() -> Path:
    return root() / "board_order.json"


def board_order() -> dict[str, list[str]]:
    """{stage: [comparison id]} as the broker arranged the board. A missing or damaged file is an
    empty order (the board then falls back to newest first); it is only a display preference."""
    try:
        raw = read_json(_order_path())
    except (OSError, ValueError):
        return {s: [] for s in STAGES}
    raw = raw if isinstance(raw, dict) else {}
    return {s: [x for x in raw.get(s, []) if isinstance(x, str)] if isinstance(raw.get(s), list) else []
            for s in STAGES}


def place(cid: str, stage: str, index: int | None = 0) -> None:
    """Put a comparison at `index` in a board column (None: at the end), out of any other column."""
    if stage not in STAGES:
        raise ValueError(stage)
    with _order_lock:
        order = board_order()
        for ids in order.values():
            while cid in ids:
                ids.remove(cid)
        ids = order[stage]
        ids.insert(len(ids) if index is None else max(0, min(index, len(ids))), cid)
        live = {d.name for d in root().iterdir() if d.is_dir()} if root().exists() else set()
        write_json(_order_path(), {s: [x for x in order[s] if x in live] for s in STAGES})


def forget(cid: str) -> None:
    with _order_lock:
        order = board_order()
        if any(cid in ids for ids in order.values()):
            write_json(_order_path(), {s: [x for x in ids if x != cid] for s, ids in order.items()})


def delete(cid: str) -> None:
    """Remove the comparison's folder and the extraction cache entries of its files."""
    with lock(cid):
        try:
            keys = {f.get("cache_key") for f in load(cid).get("files", [])} - {None}
        except Damaged:
            keys = set()  # a damaged comparison can still be deleted
        shutil.rmtree(folder(cid))
    in_use = {f.get("cache_key") for c in _all_raw() for f in c.get("files", [])}
    for key in keys - in_use:  # the same file may sit in another comparison
        delete_cache_entry(key)
    with _locks_guard:
        _locks.pop(cid, None)
    forget(cid)


def _all_raw() -> list[dict]:
    out = []
    for d in root().iterdir() if root().exists() else []:
        try:
            out.append(read_json(d / "comparison.json"))
        except (OSError, ValueError):
            pass
    return out


def delete_cache_entry(key: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", key or ""):
        (ROOT / settings()["cache_dir"] / f"{key}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- files

def new_file_id() -> str:
    return "f" + secrets.token_hex(4)


def file_path(cid: str, f: dict) -> Path:
    return folder(cid) / "files" / f"{f['id']}{f['ext']}"


def pages_dir(cid: str) -> Path:
    return folder(cid) / "pages"


def write_work(cid: str, fid: str, kind: str, obj) -> None:
    """Save a work file, only while the comparison exists: a comparison deleted mid-read must not
    come back as a folder of leftovers (the page text in it is the client's)."""
    with lock(cid):
        find_file(load(cid), fid)  # NotFound when the comparison or the file is gone
        write_json(work_path(cid, fid, kind), obj)


def remove_orphan(cid: str, cache_key: str | None = None) -> None:
    """After a comparison was deleted mid-read: remove what the read left behind (page images,
    the folder) and its new cache entry, unless another comparison uses the same file."""
    with lock(cid):
        d = folder(cid)
        if d.exists() and not (d / "comparison.json").exists():
            shutil.rmtree(d, ignore_errors=True)
    if cache_key and cache_key not in {f.get("cache_key") for c in _all_raw() for f in c.get("files", [])}:
        delete_cache_entry(cache_key)


def work_path(cid: str, fid: str, kind: str) -> Path:
    if not FILE_ID_RE.match(fid):
        raise NotFound(fid)
    return folder(cid) / "work" / f"{fid}.{kind}.json"


def page_png(cid: str, fid: str, n: int) -> Path:
    if not FILE_ID_RE.match(fid) or not (1 <= n <= 999):
        raise NotFound(fid)
    return pages_dir(cid) / f"{fid}_p{n}.png"


def find_file(comp: dict, fid: str) -> dict:
    for f in comp["files"]:
        if f["id"] == fid:
            return f
    raise NotFound(fid)


def remove_file_artifacts(cid: str, f: dict) -> None:
    file_path(cid, f).unlink(missing_ok=True)
    for kind in ("pages", "extraction"):
        work_path(cid, f["id"], kind).unlink(missing_ok=True)
    for png in pages_dir(cid).glob(f"{f['id']}_p*.png"):
        png.unlink(missing_ok=True)
