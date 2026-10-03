"""The background worker: runs steps 1 to 4 for one uploaded file at a time.

One thread, one queue. No parallel OCR or LLM calls. Each stage is written to comparison.json
as it starts (files[].status), so the UI's progress view shows the real pipeline stage, and a
page reload or a server restart never loses track of a file:

    queued -> reading -> redacting -> extracting -> checking -> done | empty | failed
    (a restart marks a file that was mid-way as `interrupted`; the broker presses Retry)

Job kinds:
    process   the whole pipeline for a new upload (or a retry)
    recheck   extraction + checks again from the saved page text (lines.yaml or prompt changed)
    move      the broker says a quote is for another line: extraction again with that correction
"""
from __future__ import annotations

import logging
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from app import extract, llm, read, redact, storage, verify
from app.build import display_carrier
from app.config import lines_config, settings

log = logging.getLogger("quote_compare.worker")

# Broker-facing messages. The technical detail goes to the log and the /debug page only.
MSG_OPEN = "This file could not be opened. It may be damaged or password-protected."
MSG_NO_TEXT = "No readable text was found in this file."
MSG_TOO_MANY_PAGES = "This file has {n} pages, more than the {limit} the app reads. Add only the quote pages."
MSG_NO_KEY = "The app is not connected to the quote reader yet. Add the API key (see README), then press Retry."
MSG_BUDGET = "Today's spending limit for reading quotes is used up. Try again tomorrow, or raise the limit in settings."
MSG_BUDGET_MONTH = "This month's spending limit for reading quotes is used up. Raise the limit in settings, or wait for next month."
MSG_LEDGER = ("The app's spending record (data/llm_usage.json) is damaged, so reading is paused to keep the "
              "spending limits safe. Restore the file from a backup (or delete it to start the record again), then press Retry.")
MSG_API = "The quote reader could not be reached. Check the internet connection, then press Retry."
MSG_KEY_REFUSED = ("The quote reader turned down the app's API key. Put a new key in the .env file, restart the app, "
                   "then press Retry.")
MSG_MODEL_GONE = ("The quote reader's model is no longer available. The model in config/settings.yaml needs "
                  "updating to a current one; then press Retry.")
MSG_PROVIDER = "The quote reader is having problems right now. Press Retry in a few minutes."
MSG_UNREADABLE = "The quote could not be read reliably. Press Retry, or type the values in yourself."
MSG_INTERRUPTED = "The app was closed while this file was being read. Press Retry."
MSG_UNEXPECTED = "Something went wrong while reading this file. Press Retry."
MSG_EMPTY = "No insurance quote was found in this file."


class StageError(RuntimeError):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


# ---------------------------------------------------------------- pipeline hooks (tests replace these)

def read_pages(path: Path, pages_dir: Path) -> list[dict]:
    return read.read_file(path, pages_dir)


def client_for(comp: dict, f: dict) -> redact.Client:
    c = comp["client"]
    return redact.Client(c["name"], c.get("address", ""), c.get("other_names") or [], c.get("property_address", ""))


def run_extract(pages: list[dict], hint: str | None) -> dict:
    return extract.extract(pages, hint=hint)


# ---------------------------------------------------------------- helpers

def _set_status(cid: str, fid: str, status: str, **fields) -> None:
    def change(comp):
        f = storage.find_file(comp, fid)
        f["status"] = status
        f.update(fields)
    storage.update(cid, change)


def summary(quotes: list[dict]) -> str:
    """'Travelers: home and auto quotes' / 'Orion180: home quote'."""
    labels = {k: v["label"] if v["label"].isupper() else v["label"].lower() for k, v in lines_config()["lines"].items()}
    by_carrier: dict[str, list[str]] = {}
    for q in quotes:
        by_carrier.setdefault(display_carrier(q.get("carrier")) or "Unknown carrier", []).append(
            labels.get(q.get("line"), "other"))
    parts = []
    for carrier, lines in by_carrier.items():
        uniq = list(dict.fromkeys(lines))
        what = " and ".join(uniq) if len(uniq) <= 2 else ", ".join(uniq[:-1]) + " and " + uniq[-1]
        parts.append(f"{carrier}: {what} quote{'s' if len(lines) > 1 else ''}")
    return "; ".join(parts)


def locate_pages(quotes: list[dict], pages: list[dict]) -> None:
    """Page number of each source line, for "As printed on the quote, page N" and the page image."""
    per_page = [(p["page"], verify.PageText([p])) for p in pages]

    def page_of(source):
        if not source:
            return None
        return next((n for n, pt in per_page if pt.contains(source)), None)

    for q in quotes:
        for it in q.get("items", []):
            it["page"] = page_of(it.get("source"))
        for n in q.get("notices", []):
            n["page"] = page_of(n.get("source"))
        if q.get("price"):
            q["price"]["page"] = page_of(q["price"].get("source"))
        q["pages"] = q.get("pages") or sorted({it["page"] for it in q.get("items", []) if it.get("page")}
                            | ({q["price"]["page"]} if (q.get("price") or {}).get("page") else set()))


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def apply_line_hints(quotes: list[dict], hints: list[dict]) -> list[dict]:
    """Force the broker's line choice on the matching quote (same carrier and price), in case the
    re-extraction did not follow the correction. Unknown keys for the new line become `other`."""
    for h in hints:
        for q in quotes:
            if _norm(q.get("carrier")) == _norm(h["carrier"]) and \
                    _norm((q.get("price") or {}).get("value")) == _norm(h.get("price")) and q["line"] != h["line"]:
                q["line"] = h["line"]
                q.update(extract.normalize({"quotes": [q]})["quotes"][0])
    return quotes


def hint_text(hints: list[dict]) -> str | None:
    if not hints:
        return None
    labels = {k: v["label"] for k, v in lines_config()["lines"].items()}
    out = []
    for h in hints:
        line_label = labels.get(h["line"], "other (not one of the configured lines)")
        price = f" (price {h['price']})" if h.get("price") else ""
        out.append(f'The {h["carrier"]} quote in this document{price} is a {line_label} quote. '
                   f'Record it with line "{h["line"]}" and that line\'s keys.')
    return "\n".join(out)


# ---------------------------------------------------------------- the job

def _read_and_redact(cid: str, comp: dict, f: dict) -> list[dict]:
    limit = settings()["app"]["max_pages_per_file"]
    _set_status(cid, f["id"], "reading", error=None, error_detail=None)
    tmp_pages = storage.pages_dir(cid) / f".{f['id']}.tmp"
    shutil.rmtree(tmp_pages, ignore_errors=True)
    try:
        pages = read_pages(storage.file_path(cid, f), tmp_pages)
    except Exception as e:
        raise StageError(MSG_OPEN, f"read: {type(e).__name__}: {e}") from e
    if len(pages) > limit:
        raise StageError(MSG_TOO_MANY_PAGES.format(n=len(pages), limit=limit))
    if not any(p["text"].strip() for p in pages):
        raise StageError(MSG_NO_TEXT)
    # page images move to their final names: pages/<fid>_p<n>.png
    for p in pages:
        dest = storage.page_png(cid, f["id"], p["page"])
        shutil.move(p["png"], dest)
        p["png"] = dest.name
    shutil.rmtree(tmp_pages, ignore_errors=True)

    _set_status(cid, f["id"], "redacting", pages=[{"page": p["page"], "source": p["source"]} for p in pages])
    red, count = redact.redact_pages(pages, client_for(comp, f))
    storage.write_work(cid, f["id"], "pages", red)
    _set_status(cid, f["id"], "extracting", redactions=count)
    return red


def _extract(cid: str, f: dict, pages: list[dict]) -> dict:
    hints = f.get("line_hints") or []
    try:
        rec = run_extract(pages, hint_text(hints))
    except llm.LedgerDamaged as e:
        raise StageError(MSG_LEDGER, str(e)) from e
    except llm.BudgetExceeded as e:
        raise StageError(MSG_BUDGET_MONTH if "monthly" in str(e) else MSG_BUDGET, str(e)) from e
    except extract.ExtractionFailed as e:
        msg = str(e)
        if "is not set" in msg:
            raise StageError(MSG_NO_KEY, msg) from e
        if msg.startswith("LLM call failed"):
            kind = llm.failure_kind(e.__cause__)
            raise StageError({"key": MSG_KEY_REFUSED, "model": MSG_MODEL_GONE,
                              "provider": MSG_PROVIDER}.get(kind, MSG_API), msg) from e
        raise StageError(MSG_UNREADABLE, msg) from e
    storage.write_work(cid, f["id"], "extraction", rec)
    return rec


def run_job(cid: str, fid: str, kind: str) -> None:
    try:
        comp = storage.load(cid)
        f = storage.find_file(comp, fid)
    except storage.NotFound:
        return  # removed while waiting in the queue
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("file %s/%s: comparison.json could not be read", cid, fid)
        return  # a damaged comparison.json: this file waits; the next start marks it for Retry
    t0 = time.perf_counter()
    rec = None
    try:
        saved = storage.work_path(cid, fid, "pages")
        if kind == "process" or not saved.exists():  # nothing saved to re-check from: run everything
            pages = _read_and_redact(cid, comp, f)
        else:
            _set_status(cid, fid, "extracting", error=None, error_detail=None)
            pages = storage.read_json(saved)
        rec = _extract(cid, f, pages)

        _set_status(cid, fid, "checking")
        quotes = verify.verify(rec["quotes"], pages)
        quotes = apply_line_hints(quotes, f.get("line_hints") or [])
        locate_pages(quotes, pages)
        meta = rec.get("meta", {})
        finish(cid, fid, quotes, meta, time.perf_counter() - t0)
    except storage.NotFound:  # the comparison or the file was removed while it was being read
        _discard_leftovers(cid, f, (rec or {}).get("meta", {}).get("cache_key"))
    except StageError as e:
        log.warning("file %s/%s failed: %s | %s", cid, fid, e, e.detail)
        if kind == "recheck" and f.get("quote_ids"):
            # an automatic re-check that failed keeps the values read before; not retried until
            # the next version change, so a missing key or a spending cap cannot cause a loop
            _safe_status(cid, fid, "done", versions=current_versions(), error=None,
                         error_detail=f"re-check failed: {e.detail or e}"[:500])
        else:
            _safe_status(cid, fid, "failed", error=str(e), error_detail=e.detail[:500])
    except Exception as e:  # never let one file stop the worker
        log.exception("file %s/%s: unexpected error", cid, fid)
        _safe_status(cid, fid, "failed", error=MSG_UNEXPECTED, error_detail=f"{type(e).__name__}: {e}"[:500])


def _discard_leftovers(cid: str, f: dict, cache_key: str | None) -> None:
    try:
        if storage.find_file(storage.load(cid), f["id"]):
            return  # still there after all
    except storage.NotFound:
        pass
    except Exception:
        return
    try:
        storage.remove_file_artifacts(cid, f)
        storage.remove_orphan(cid, cache_key)
    except Exception:
        log.exception("could not remove what %s/%s left behind", cid, f["id"])


def _safe_status(cid: str, fid: str, status: str, **fields) -> None:
    try:
        _set_status(cid, fid, status, **fields)
    except storage.NotFound:
        pass
    except Exception:  # a full disk or a damaged file: log it, never stop the worker
        log.exception("could not save status %s for %s/%s", status, cid, fid)


def finish(cid: str, fid: str, quotes: list[dict], meta: dict, seconds: float) -> None:
    def change(comp):
        f = storage.find_file(comp, fid)
        old = [q for q in comp["quotes"] if q["file_id"] == fid]
        old_ids = {q["id"] for q in old}
        ids = _keep_ids(old, quotes, fid)
        new = [{**q, "id": qid, "file_id": fid, "file_name": f["name"]} for q, qid in zip(quotes, ids)]
        new_ids = {q["id"] for q in new}
        # keep upload order: this file's quotes go where its old quotes were (or at the end)
        pos = next((i for i, q in enumerate(comp["quotes"]) if q["file_id"] == fid), len(comp["quotes"]))
        rest = [q for q in comp["quotes"] if q["file_id"] != fid]
        pos = min(pos, len(rest))
        comp["quotes"] = rest[:pos] + new + rest[pos:]
        gone = old_ids - new_ids
        comp["edits"] = [e for e in comp["edits"] if e["quote_id"] not in gone]
        by_id = {q["id"]: q for q in comp["quotes"]}
        comp["recommendation"] = {line: qid for line, qid in comp["recommendation"].items()
                                  if qid in by_id and by_id[qid]["line"] == line}
        comp["current"] = {line: qid for line, qid in comp["current"].items()
                           if qid in by_id and by_id[qid]["line"] == line}
        f.update(status="done" if new else "empty", error=None if new else MSG_EMPTY, error_detail=None,
                 summary=summary(new) if new else None, quote_ids=[q["id"] for q in new],
                 cache_key=meta.get("cache_key"), versions=current_versions(),
                 seconds=round(seconds, 1), model=meta.get("model"))
    storage.update(cid, change)


def _keep_ids(old: list[dict], quotes: list[dict], fid: str) -> list[str]:
    """Ids for a file's quotes after a re-read. The broker's edits, pick, and current policy are
    keyed by quote id, so each quote keeps its old id when it is recognisably the same quote, even
    if the model lists the quotes in another order or drops or adds one:
    1. same line and carrier (several options from one carrier on one line: in order);
    2. then same carrier on any line (the broker moved the quote to another line);
    3. anything left gets a number never used by this file's current quotes."""
    pair = lambda q: (q["line"], _norm(q.get("carrier")))
    by_pair: dict = {}
    for q in old:
        by_pair.setdefault(pair(q), []).append(q["id"])
    ids: list = [by_pair[pair(q)].pop(0) if by_pair.get(pair(q)) else None for q in quotes]
    used = set(ids)
    by_carrier: dict = {}
    for q in old:
        if q["id"] not in used:
            by_carrier.setdefault(_norm(q.get("carrier")), []).append(q["id"])
    n = max([int(i.rsplit("-", 1)[1]) for i in (q["id"] for q in old) if i.rsplit("-", 1)[1].isdigit()] + [0])
    for j, q in enumerate(quotes):
        if ids[j] is None:
            pool = by_carrier.get(_norm(q.get("carrier")))
            if pool:
                ids[j] = pool.pop(0)
            else:
                n += 1
                ids[j] = f"{fid}-{n}"
    return ids


def current_versions() -> dict:
    return {"config": str(lines_config()["config_version"]), "prompt": str(settings()["prompt_version"])}


# ---------------------------------------------------------------- the queue

class Worker:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.pending: set[tuple[str, str]] = set()
        self.again: set[tuple[str, str]] = set()  # asked for again while running (Retry right as it finished)
        self.running: tuple[str, str] | None = None
        self.draining = threading.Event()  # shutting down: finish the current file, start no other
        self._guard = threading.Lock()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.recover()
        self.thread = threading.Thread(target=self._loop, name="quote-worker", daemon=True)
        self.thread.start()

    def enqueue(self, cid: str, fid: str, kind: str = "process") -> None:
        with self._guard:
            if (cid, fid) in self.pending:
                self.again.add((cid, fid))
                return
            self.pending.add((cid, fid))
        self.q.put((cid, fid, kind))

    def finish_current(self, timeout: float) -> bool:
        """Before a shutdown: let the file being read finish (a model call already paid for is not
        thrown away), and start no other; files still waiting stay queued for the next start."""
        self.draining.set()
        end = time.monotonic() + timeout
        while self.running and time.monotonic() < end:
            time.sleep(0.1)
        return self.running is None

    def stop(self, timeout: float = 5) -> None:
        self.q.put(None)
        if self.thread:
            self.thread.join(timeout)

    def idle(self) -> bool:
        with self._guard:
            return not self.pending

    def _loop(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                return
            cid, fid, kind = job
            if self.draining.is_set():
                with self._guard:
                    self.pending.discard((cid, fid))
                continue
            try:
                with self._guard:
                    self.running = (cid, fid)
                run_job(cid, fid, kind)
            except Exception:  # last line of defence: the worker thread must outlive any one file
                log.exception("worker: job %s/%s crashed", cid, fid)
            finally:
                with self._guard:
                    self.running = None
                    self.pending.discard((cid, fid))
                    again = (cid, fid) in self.again
                    self.again.discard((cid, fid))
                if again:
                    self._run_again(cid, fid)

    def _run_again(self, cid: str, fid: str) -> None:
        """A Retry or Move that came in while this file was finishing set it back to "queued": run it."""
        try:
            f = storage.find_file(storage.load(cid), fid)
        except Exception:
            return
        if f["status"] == "queued":
            self.enqueue(cid, fid, f.get("job_kind") or "process")

    def recover(self) -> None:
        """At startup: files that were waiting are queued again; a file that was mid-way is
        marked interrupted (re-running it could spend on the API without the broker asking)."""
        for c in storage.list_all():
            try:
                comp = storage.load(c["id"])
            except (storage.NotFound, ValueError):
                continue
            for f in comp["files"]:
                if f["status"] == "queued":
                    self.enqueue(comp["id"], f["id"], f.get("job_kind") or "process")
                elif f["status"] in storage.ACTIVE:
                    _safe_status(comp["id"], f["id"], "interrupted", error=MSG_INTERRUPTED)


worker = Worker()
