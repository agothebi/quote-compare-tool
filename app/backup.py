"""Backups of the clients, outside the app folder, so deleting or re-downloading the app loses nothing.

Where: Documents/Quote Compare Backups (the real Documents folder on Windows, also when OneDrive
moves it), or QUOTE_COMPARE_BACKUP_DIR in .env, or backup.dir in config/settings.yaml.

When: as the app starts, then every hour while it runs, but only when something changed; never
while there are no clients (an emptied folder must not push the good backups out).

How it stays small: every file is stored once, named by its content (blobs/ab/<sha256>), and a
backup is a list of paths and blobs (snapshots/<date>_<time>.json). A quote file or page image is
stored once however many backups hold it; an hourly backup after an edit adds only the changed
comparison.json. Old backups are thinned (every one from today, the last of each of the 14 days
before, the last of each of the 12 months before) and blobs no backup uses are deleted.

Restoring puts the backup's files back in data/ and removes clients the backup doesn't have, after
backing up the current state first (so a restore can be undone). It is refused while files are
being read, or when a blob it needs is missing (nothing is changed then).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import config, storage

log = logging.getLogger("backup")

FOLDER_NAME = "Quote Compare Backups"
DIR_ENV = "QUOTE_COMPARE_BACKUP_DIR"
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?$")  # a backup's id: its file name, no ':' (Windows)
KEEP_DAYS, KEEP_MONTHS = 14, 12
LOCK_STALE_SECONDS = 120   # a running backup refreshes its lock; an older one was left by a closed window
AUTO = True                  # the hourly backup; the tests turn it off
_lock = threading.RLock()    # one backup or restore at a time in this app
_state = {"last_ok": None, "last_error": None, "last_error_at": None}


class BackupError(Exception):
    """A backup or restore that could not be done; the message is for the broker."""


class Busy(BackupError):
    pass


# ---------------------------------------------------------------- where

def _windows_documents() -> Path | None:
    """The Documents folder as Windows knows it: also right when OneDrive or a policy moved it."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
        guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # not Windows, or an old one: the fallback below
        return None


def _on_windows() -> bool:
    return os.name == "nt"


def backup_dir() -> Path:
    config.load_env()
    chosen = (os.environ.get(DIR_ENV) or "").strip() or ((config.settings().get("backup") or {}).get("dir") or "")
    if chosen:
        return Path(os.path.expandvars(os.path.expanduser(str(chosen))))
    docs = _windows_documents() if _on_windows() else None
    if docs is None:
        docs = Path.home() / "Documents"
        if not docs.is_dir():
            docs = Path.home()
    return docs / FOLDER_NAME


def _snapshots(base: Path) -> Path:
    return base / "snapshots"


def _blob(base: Path, sha: str) -> Path:
    return base / "blobs" / sha[:2] / sha


# ---------------------------------------------------------------- what

def _sources() -> list[tuple[str, Path]]:
    """(path in the backup, file on disk): the clients, the board order, the email text, the spend record."""
    out = []
    root = storage.root()
    if root.exists():
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if p.is_file() and not any(part.startswith(".") for part in rel.parts):  # .x.tmp: a write in progress
                out.append(("comparisons/" + rel.as_posix(), p))
    from app import llm
    for rel, p in (("email.txt", config.EMAIL_FILE), ("llm_usage.json", llm.USAGE_FILE)):
        if Path(p).is_file():
            out.append((rel, Path(p)))
    return out


def _clients(files: dict) -> int:
    return sum(1 for rel in files if re.fullmatch(r"comparisons/[^/]+/comparison\.json", rel))


# ---------------------------------------------------------------- small file helpers (Windows-safe)

def _retry(fn, *args):
    """Windows: a virus scanner, the indexer or OneDrive can hold a file for a moment."""
    for attempt in range(6):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _retry(os.replace, tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _DirLock:
    """One backup or restore at a time across processes sharing the folder (two copies of the app)."""

    def __init__(self, base: Path):
        self.path = base / ".lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:  # left by a crash
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                raise Busy("Another copy of Quote Compare is backing up right now. Try again in a minute.")
        raise Busy("The backup folder is locked. Try again in a minute.")

    def touch(self) -> None:
        """Still working: keeps a long first backup's lock from looking abandoned."""
        try:
            os.utime(self.path)
        except OSError:
            pass

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- reading backups

# A backup file = one line of summary (what the list shows, read alone), then the file list.

def _read_manifest(path: Path) -> dict | None:
    try:
        head, _, body = path.read_text(encoding="utf-8").partition("\n")
        m = json.loads(head)
        m["files"] = json.loads(body)
        return m if isinstance(m, dict) and isinstance(m.get("files"), dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _read_summary(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            m = json.loads(f.readline())
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def _order(p: Path) -> tuple:
    """Oldest first: by time, then the -2, -3 of backups made in the same second ('-' sorts before '.')."""
    stamp, n = p.stem[:17], p.stem[18:]  # 2026-10-03_161205 and the 2 of -2
    return stamp, int(n) if n.isdigit() else 1


def _manifests(base: Path) -> list[tuple[str, dict]]:
    """(id, manifest), oldest first; unreadable ones are skipped."""
    d = _snapshots(base)
    out = []
    for p in sorted(d.glob("*.json"), key=_order) if d.is_dir() else []:
        if ID_RE.match(p.stem):
            m = _read_manifest(p)
            if m is not None:
                out.append((p.stem, m))
    return out


def list_backups() -> dict:
    base = backup_dir()
    d = _snapshots(base)
    items = []
    for p in sorted(d.glob("*.json"), key=_order, reverse=True) if d.is_dir() else []:
        m = _read_summary(p) if ID_RE.match(p.stem) else None
        if m is not None:
            items.append({"id": p.stem, "created_at": m.get("created_at"), "clients": m.get("clients", 0),
                          "reason": m.get("reason", "auto"), "size": m.get("size", 0)})
    return {"dir": str(base), "backups": items, "last_ok": _state["last_ok"],
            "last_error": _state["last_error"], "last_error_at": _state["last_error_at"]}


def last_error() -> str | None:
    """Why the last backup failed (the board shows it), or None."""
    return _state["last_error"]


def store_size(base: Path | None = None) -> int:
    """Bytes the backup folder takes on disk."""
    base = base or backup_dir()
    return sum(p.stat().st_size for p in base.rglob("*") if p.is_file()) if base.exists() else 0


# ---------------------------------------------------------------- making a backup

def run(reason: str = "auto", now: datetime | None = None) -> dict | None:
    """Back up what changed since the last backup. Returns the new backup's entry, or None when
    nothing changed or there are no clients. Raises BackupError (also recorded for the page)."""
    try:
        with _lock:
            base = backup_dir()
            with _DirLock(base) as lock:
                out = _run(base, reason, now or datetime.now().astimezone(), lock)
        _state.update(last_ok=datetime.now().astimezone().isoformat(timespec="seconds"), last_error=None, last_error_at=None)
        return out
    except Busy:
        raise
    except Exception as e:
        msg = _explain(e)
        _state.update(last_error=msg, last_error_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        log.warning("backup failed: %s", e)
        raise BackupError(msg) from e


def _explain(e: Exception) -> str:
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "The backup couldn't be saved: the disk is full."
    if isinstance(e, PermissionError):
        return "The backup folder can't be written to. Check that it isn't read-only."
    return f"The backup couldn't be saved ({type(e).__name__})."


def _run(base: Path, reason: str, now: datetime, lock: _DirLock | None = None) -> dict | None:
    known = _manifests(base)
    last = known[-1][1]["files"] if known else {}
    files = {}
    for n, (rel, path) in enumerate(_sources()):
        if lock and n % 25 == 0:
            lock.touch()
        try:
            st = path.stat()
            prev = last.get(rel)
            if prev and prev.get("size") == st.st_size and prev.get("mtime_ns") == st.st_mtime_ns \
                    and _blob(base, prev["sha"]).exists():
                files[rel] = prev  # unchanged since the last backup: not read again
                continue
            data = _retry(path.read_bytes)
        except FileNotFoundError:
            continue  # deleted while the backup ran (a client deleted, a write finishing)
        sha = hashlib.sha256(data).hexdigest()
        blob = _blob(base, sha)
        if not blob.exists() or blob.stat().st_size != len(data):
            _write_atomic(blob, data)
        files[rel] = {"sha": sha, "size": len(data), "mtime_ns": st.st_mtime_ns}
    if not _clients(files):
        return None  # nothing to protect, and an emptied folder must not push the good backups out
    if known and {k: v["sha"] for k, v in files.items()} == {k: v["sha"] for k, v in last.items()}:
        return None  # nothing changed
    bid = now.strftime("%Y-%m-%d_%H%M%S")
    n = 1
    while (_snapshots(base) / f"{bid}.json").exists():
        n += 1
        bid = f"{now.strftime('%Y-%m-%d_%H%M%S')}-{n}"
    summary = {"version": 1, "created_at": now.isoformat(timespec="seconds"), "reason": reason,
               "clients": _clients(files), "size": sum(f["size"] for f in files.values())}
    _write_atomic(_snapshots(base) / f"{bid}.json",
                  (json.dumps(summary) + "\n" + json.dumps(files, separators=(",", ":"))).encode("utf-8"))
    manifest = {**summary, "files": files}
    _write_readme(base)
    _prune(base, now)
    return {"id": bid, "created_at": manifest["created_at"], "clients": manifest["clients"], "reason": reason}


def _write_readme(base: Path) -> None:
    p = base / "README.txt"
    if not p.exists():
        p.write_text("Backups of Quote Compare's clients, made by the app while it runs.\n"
                     "To restore one, open Quote Compare and click Backups at the bottom of the board.\n"
                     "Please don't edit or rename the files in here.\n", encoding="utf-8")


def _prune(base: Path, now: datetime) -> None:
    """Keep every backup from today, the last of each of the 14 days before, the last of each of the
    12 months before, and always the newest; then delete the blobs no kept backup uses."""
    known = _manifests(base)
    if not known:
        return
    today = now.date()
    keep, by_day, by_month = {known[-1][0]}, {}, {}
    for bid, _m in known:  # oldest first: the later one of a day / month wins
        day = datetime.strptime(bid[:10], "%Y-%m-%d").date()
        if day >= today:
            keep.add(bid)
        elif day >= today - timedelta(days=KEEP_DAYS):
            by_day[day] = bid
        else:
            by_month[(day.year, day.month)] = bid
    keep |= set(by_day.values())
    months = sorted(by_month)[-KEEP_MONTHS:]
    keep |= {by_month[k] for k in months}
    for bid, _m in known:
        if bid not in keep:
            try:
                _retry((_snapshots(base) / f"{bid}.json").unlink)
            except FileNotFoundError:
                pass
    for tmp in _snapshots(base).glob(".*.tmp"):  # a backup cut off by a closed window
        try:
            if time.time() - tmp.stat().st_mtime > 3600:
                tmp.unlink()
        except OSError:
            pass
    used = {f["sha"] for bid, m in _manifests(base) for f in m["files"].values() if isinstance(f, dict) and f.get("sha")}
    blobs = base / "blobs"
    for p in blobs.rglob("*") if blobs.exists() else []:
        if p.is_file() and p.name not in used:
            try:
                _retry(p.unlink)
            except OSError:
                pass  # held open somewhere: deleted next time


# ---------------------------------------------------------------- restoring

def restore(bid: str) -> dict:
    """Put backup `bid` back. Returns {"clients": n, "undo": id of the backup of the state it replaced or None}."""
    from app import worker
    if not ID_RE.match(bid or ""):
        raise storage.NotFound(bid)
    if not worker.worker.idle():
        raise Busy("Wait until the files being read are finished, then restore.")
    with _lock:
        base = backup_dir()
        m = _read_manifest(_snapshots(base) / f"{bid}.json")
        if m is None:
            raise storage.NotFound(bid)
        missing = [rel for rel, f in m["files"].items()
                   if not (isinstance(f, dict) and _blob(base, f.get("sha", "")).is_file()
                           and _blob(base, f["sha"]).stat().st_size == f.get("size"))]
        if missing:
            raise BackupError(f"This backup is incomplete ({len(missing)} files are missing from the backup folder). "
                              "Pick another one.")
        undo = run("before restore")  # the state being replaced: a restore can be undone
        undo_id = undo["id"] if undo else (_manifests(base)[-1][0] if _clients({r: 1 for r, _ in _sources()}) else None)
        with _DirLock(base):
            _put_back(base, m)
    _after_restore()
    log.info("restored backup %s (%d clients)", bid, m.get("clients", 0))
    return {"clients": _clients(m["files"]), "undo": undo_id if undo_id != bid else None}


def _put_back(base: Path, m: dict) -> None:
    from app import llm
    root = storage.root()
    root.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for rel, f in m["files"].items():
        if rel.startswith("comparisons/"):
            parts = rel.split("/")[1:]
            if not parts or any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts):
                continue  # never write outside the clients folder
            target = root.joinpath(*parts)
        elif rel == "email.txt":
            target = Path(config.EMAIL_FILE)
        elif rel == "llm_usage.json":
            target = Path(llm.USAGE_FILE)
            if target.exists():
                continue  # the current spend record stays: an older one would under-count this month
        else:
            continue
        wanted.add(target.resolve())
        data = _blob(base, f["sha"]).read_bytes()
        try:
            if target.is_file() and target.stat().st_size == len(data) and target.read_bytes() == data:
                continue
        except OSError:
            pass
        storage.atomic_write(target, data)
    # clients the backup doesn't have (they are in the backup made just before)
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_file() and p.resolve() not in wanted:
                _retry(p.unlink)
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError as e:
            log.warning("restore: could not remove %s: %s", p, e)


def _after_restore() -> None:
    """Nothing is waiting in the reading queue for a restored file: a file caught mid-read is
    marked interrupted (Retry), never read again on its own. Cached pages and cards are dropped."""
    from app import export_pdf, present, worker
    present._card_cache.clear()
    export_pdf._preview_cache.clear()
    for c in storage.list_all():
        try:
            comp = storage.load(c["id"])
        except (storage.NotFound, storage.Damaged):
            continue
        if any(f.get("status") in storage.ACTIVE for f in comp["files"]):
            def change(x):
                for f in x["files"]:
                    if f.get("status") in storage.ACTIVE:
                        f.update(status="interrupted", error=worker.MSG_INTERRUPTED)
            storage.update(c["id"], change)


# ---------------------------------------------------------------- the hourly timer

class _Timer:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        cfg = config.settings().get("backup") or {}
        if not AUTO or cfg.get("enabled") is False or (self.thread and self.thread.is_alive()):
            return
        minutes = max(1, int(cfg.get("every_minutes") or 60))
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, args=(minutes * 60,), name="backup", daemon=True)
        self.thread.start()

    def _loop(self, every: float, first: float = 5) -> None:
        delay = first  # the first backup right after the app starts, once it is up
        while not self.stop_event.wait(delay):
            try:
                run("auto")
            except BackupError:
                pass  # recorded; the board shows it
            delay = every

    def stop(self, final: bool = True) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        if final and AUTO and (config.settings().get("backup") or {}).get("enabled") is not False:
            try:
                run("auto")  # what changed in the last hour, when the app is closed properly
            except BackupError:
                pass


timer = _Timer()
