"""The local web app: FastAPI routes + the static UI.

    python -m app.main            # http://127.0.0.1:8000

Local only: listens on 127.0.0.1, accepts only localhost Host headers (DNS rebinding), and
every state-changing request must carry the X-Quote-Compare header, which a page on another
site cannot send without a CORS preflight that this server never grants (CSRF).
"""
from __future__ import annotations

import hashlib
import logging
import logging.handlers
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app import present, storage, version
from app.config import DATA_DIR, ROOT, settings
from app.worker import current_versions, worker

STATIC = ROOT / "static"
CSRF_HEADER = "x-quote-compare"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SUFFIXES = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

log = logging.getLogger("quote_compare")


def setup_logging() -> None:
    logs = DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("quote_compare")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(logs / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(logging.StreamHandler())


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    storage.root().mkdir(parents=True, exist_ok=True)
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="Quote Compare", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- security + errors

@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"error": "This app only answers on localhost."}, status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get(CSRF_HEADER) != "1" or (
                origin and re.sub(r"^https?://", "", origin).rsplit(":", 1)[0] not in ALLOWED_HOSTS):
            return JSONResponse({"error": "Request refused."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-App-Build"] = version.RUNNING
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(storage.NotFound)
async def not_found(request: Request, exc: storage.NotFound):
    return JSONResponse({"error": "Not found. It may have been removed."}, status_code=404)


@app.exception_handler(storage.Damaged)
async def damaged(request: Request, exc: storage.Damaged):
    log.error("comparison.json is damaged: %s", exc)
    return JSONResponse({"error": "This comparison's saved file is damaged, so it can't be opened. Restore its "
                                  "comparison.json from a backup, or delete the comparison."}, status_code=500)


@app.exception_handler(storage.Conflict)
async def conflict(request: Request, exc: storage.Conflict):
    return JSONResponse({"error": str(exc), "current": exc.current}, status_code=409)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def bad_request(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "The request was not understood."}, status_code=422)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": "Something went wrong. Try again."}, status_code=500)


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/debug/{cid}", include_in_schema=False, response_class=HTMLResponse)
def debug_page(cid: str):
    from app.debug_view import render
    return HTMLResponse(render(cid), headers={
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"})


# ---------------------------------------------------------------- version and shutdown

SHUTDOWN_HOOK = None  # set by app.launch: stops the server cleanly


@app.get("/api/version")
def get_version():
    return {"build": version.RUNNING, "stale": version.on_disk() != version.RUNNING}


@app.post("/api/shutdown")
def shutdown():
    """Lets the launcher replace an out-of-date copy. Local only (Host check + X-Quote-Compare header)."""
    import os
    import signal
    stop = SHUTDOWN_HOOK or (lambda: os.kill(os.getpid(), signal.SIGINT))

    def when_done():
        worker.finish_current(timeout=90)  # a file mid-read finishes first (its model call is paid for)
        stop()
    threading.Timer(0.3, when_done).start()
    return {"ok": True}


# ---------------------------------------------------------------- comparisons

class NewComparison(BaseModel):
    client_name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=300)
    other_names: list[str] = Field(default_factory=list, max_length=20)


class OwnReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200)
    on: StrictBool = True


class ExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    length: Literal["full", "short"] | None = None
    total: StrictBool | None = None
    gloss: StrictBool | None = None
    gtk: StrictBool | None = None


class ComparisonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a field this server does not know is an error, not ignored

    client_name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=20_000)
    base_notes: str | None = Field(default=None, max_length=20_000)
    recommend_line: str | None = Field(default=None, max_length=40)
    recommend_quote: str | None = Field(default=None, max_length=40)
    keep_line: str | None = Field(default=None, max_length=40)
    keep_key: str | None = Field(default=None, max_length=300)
    keep: bool | None = None
    reasons_line: str | None = Field(default=None, max_length=40)
    fact_id: str | None = Field(default=None, max_length=340)
    fact_on: StrictBool | None = None
    own_reasons: list[OwnReason] | None = Field(default=None, max_length=20)
    current_line: str | None = Field(default=None, max_length=40)
    current_quote: str | None = Field(default=None, max_length=40)
    stage: Literal["working", "ready", "sent", "closed"] | None = None
    outcome: Literal["bound", "lost"] | None = None
    archived: StrictBool | None = None
    export: ExportOptions | None = None


class BoardMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=100)
    stage: Literal["working", "ready", "sent", "closed"]
    index: int = Field(default=0, ge=0, le=100_000)


class CellChange(BaseModel):
    quote_id: str = Field(max_length=40)
    key: str = Field(max_length=300)
    index: int | None = None
    label: str | None = Field(default=None, max_length=300)
    vehicle: str | None = Field(default=None, max_length=200)
    action: Literal["accept", "edit", "clear", "undo"]
    value: str | None = Field(default=None, max_length=500)


class MoveQuote(BaseModel):
    line: str = Field(max_length=40)


def view(cid: str, recheck: bool = True) -> dict:
    comp = storage.load(cid)
    if recheck and settings()["app"].get("auto_recheck"):
        comp = maybe_recheck(comp)
    return present.view(comp)


def maybe_recheck(comp: dict) -> dict:
    """lines.yaml or the prompt changed since these files were read: extract them again."""
    if comp.get("stage") in ("sent", "closed") or comp.get("archived_at"):
        return comp  # a proposal already sent keeps the numbers the client saw; an archived client is left alone
    versions = current_versions()
    stale = [f["id"] for f in comp["files"] if f["status"] == "done" and f.get("versions")
             and f["versions"] != versions and storage.work_path(comp["id"], f["id"], "pages").exists()]
    if not stale:
        return comp

    def change(c):
        for f in c["files"]:
            if f["id"] in stale and f["status"] == "done":
                f.update(status="queued", job_kind="recheck")
    comp = storage.update(comp["id"], change)
    for fid in stale:
        worker.enqueue(comp["id"], fid, "recheck")
    return comp


@app.get("/api/comparisons")
def list_comparisons(archived: bool = False):
    """The board's cards (archived clients left out), or with ?archived=1 only the archived ones,
    most recently archived first. Either way, how many are archived (the board links to them)."""
    comps = storage.list_raw()
    shelf = [c for c in comps if c.get("archived_at")]
    if archived:
        comps = sorted(shelf, key=lambda c: c["archived_at"], reverse=True)
    else:
        comps = [c for c in comps if not c.get("archived_at")]
    cards = []
    for comp in comps:
        try:
            cards.append(present.card(comp))
        except Exception:  # one damaged comparison must not hide the whole board
            log.exception("could not summarize %s", comp.get("id"))
            cards.append({"id": comp["id"], "client_name": comp["client"]["name"], "stage": comp["stage"],
                          "outcome": comp["outcome"], "archived_at": comp.get("archived_at"), "created_at": comp["created_at"],
                          "updated_at": comp["updated_at"], "quotes": len(comp["quotes"]), "files": len(comp["files"]),
                          "lines": [], "carriers": [], "review": 0, "picked": 0, "total": None,
                          "total_estimated": False, "expires": None, "processing": 0, "failed": 0, "progress": None})
    return {"comparisons": cards, "archived": len(shelf)}


@app.post("/api/board/move")
def board_move(body: BoardMove):
    """Drag and drop on the board: the card's column (its status) and its place in the column."""
    comp = storage.load(body.id)
    if comp["archived_at"]:
        raise HTTPException(409, "This client is archived. Unarchive it first.")
    if comp["stage"] != body.stage:
        def change(c):
            c["stage"] = body.stage
            if body.stage != "closed":
                c["outcome"] = None
        storage.update(body.id, change)
    storage.place(body.id, body.stage, body.index)
    return list_comparisons()


@app.post("/api/comparisons", status_code=201)
def create_comparison(body: NewComparison):
    if not body.client_name.strip():
        raise HTTPException(422, "Enter the client's name.")
    comp = storage.create(body.client_name, body.address, body.other_names)
    return present.view(comp)


@app.get("/api/comparisons/{cid}")
def get_comparison(cid: str, request: Request):
    # a re-check spends money: only the app's own page (which sends the header) starts one, never
    # an <img src> on another website that guesses the id
    return view(cid, recheck=request.headers.get("x-quote-compare") == "1")


@app.patch("/api/comparisons/{cid}")
def patch_comparison(cid: str, body: ComparisonPatch):
    moved: list[str] = []
    shelved: list[bool] = []  # [True] archived by this change, [False] unarchived

    def change(comp):
        moved.clear()
        shelved.clear()
        if body.client_name is not None:
            if not body.client_name.strip():
                raise HTTPException(422, "Enter the client's name.")
            comp["client"]["name"] = body.client_name.strip()
        if body.address is not None:
            comp["client"]["address"] = body.address.strip()
        if body.notes is not None:
            if body.base_notes is not None and body.base_notes != comp.get("notes", ""):
                raise storage.Conflict("These notes were changed in another window.", comp.get("notes", ""))
            comp["notes"] = body.notes
        if body.recommend_line is not None:
            ids = {q["id"] for q in comp["quotes"] if q["line"] == body.recommend_line}
            if body.recommend_quote is None:
                comp["recommendation"].pop(body.recommend_line, None)
                comp["fact_choices"].pop(body.recommend_line, None)
                comp["own_reasons"].pop(body.recommend_line, None)
            elif body.recommend_quote in ids:
                if comp["recommendation"].get(body.recommend_line) != body.recommend_quote:
                    comp["fact_choices"].pop(body.recommend_line, None)  # the reasons were for the old pick
                    comp["own_reasons"].pop(body.recommend_line, None)
                comp["recommendation"][body.recommend_line] = body.recommend_quote
            else:
                raise storage.NotFound(body.recommend_quote)
        if body.reasons_line is not None and (body.fact_id is not None or body.own_reasons is not None):
            if body.reasons_line not in comp["recommendation"]:
                raise HTTPException(409, "Recommend a quote first.")
            if body.fact_id is not None and body.fact_on is not None:
                comp["fact_choices"].setdefault(body.reasons_line, {})[body.fact_id] = body.fact_on
            if body.own_reasons is not None:
                comp["own_reasons"][body.reasons_line] = [{"text": r.text.strip(), "on": r.on}
                                                          for r in body.own_reasons if r.text.strip()]
        if body.current_line is not None:
            ids = {q["id"] for q in comp["quotes"] if q["line"] == body.current_line}
            if body.current_quote is None:
                comp["current"].pop(body.current_line, None)
            elif body.current_quote in ids:
                comp["current"][body.current_line] = body.current_quote
            else:
                raise storage.NotFound(body.current_quote)
        if body.stage is not None and body.stage != comp["stage"]:
            comp["stage"] = body.stage
            if body.stage != "closed":
                comp["outcome"] = None
            moved.append(body.stage)
        if "outcome" in body.model_fields_set:
            if body.outcome is not None and comp["stage"] != "closed":
                raise HTTPException(409, "Only a closed comparison is bound or not bound.")
            comp["outcome"] = body.outcome
        if body.archived is not None and body.archived != bool(comp["archived_at"]):
            # archiving only takes the client off the board: its files, readings and stage are kept
            comp["archived_at"] = storage.now() if body.archived else None
            shelved.append(body.archived)
        if body.export is not None:
            comp["export"].update(body.export.model_dump(exclude_none=True))
        if body.keep_line is not None and body.keep_key is not None and body.keep is not None:
            kept = comp["kept"].setdefault(body.keep_line, [])
            if body.keep and body.keep_key not in kept:
                kept.append(body.keep_key)
            elif not body.keep and body.keep_key in kept:
                kept.remove(body.keep_key)
    comp = storage.update(cid, change)
    if comp["archived_at"]:
        storage.forget(cid)  # a status change while archived only sets the column it comes back to
    elif moved or shelved:
        storage.place(cid, comp["stage"], 0)  # moved or unarchived: at the top of its column
    return view(cid)


@app.delete("/api/comparisons/{cid}")
def delete_comparison(cid: str):
    storage.delete(cid)
    return {"ok": True}


# ---------------------------------------------------------------- files and jobs

def _job_id(cid: str, fid: str) -> str:
    return f"{cid}.{fid}"


@app.post("/api/comparisons/{cid}/files", status_code=202)
async def upload(cid: str, request: Request, file: UploadFile = File(...)):
    storage.load(cid)  # 404 before reading the body into place
    limit = settings()["app"]["max_upload_mb"] * 1024 * 1024
    name = Path(file.filename or "quote.pdf").name[:180] or "quote.pdf"
    ext = Path(name).suffix.lower()
    if ext not in SUFFIXES:
        raise HTTPException(415, f"{name}: only PDF, PNG, and JPG files can be added.")
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"{name} is larger than {settings()['app']['max_upload_mb']} MB.")
    if not bytes(data[:8]).startswith(SUFFIXES[ext]):
        raise HTTPException(415, f"{name} is not a real {ext[1:].upper()} file.")
    sha = hashlib.sha256(data).hexdigest()
    return _add_file(cid, name, ext, bytes(data), sha)


def _add_file(cid: str, name: str, ext: str, data: bytes, sha: str) -> dict:
    if ext == ".pdf":
        _check_pdf(name, data)
    else:
        _check_image(name, data)
    fid = storage.new_file_id()
    f = {"id": fid, "name": name, "ext": ext, "sha256": sha, "size": len(data), "status": "queued",
         "job_kind": "process", "added_at": storage.now(), "pages": [], "redactions": None, "quote_ids": []}

    def change(comp):
        dup = next((x for x in comp["files"] if x["sha256"] == sha), None)
        if dup:
            raise HTTPException(409, f"{name} is already in this comparison.")
        path = storage.file_path(cid, f)
        storage.atomic_write(path, data)
        comp["files"].append(f)
    storage.update(cid, change)
    worker.enqueue(cid, fid, "process")
    return {"job_id": _job_id(cid, fid), "file_id": fid}


MAX_IMAGE_PIXELS = 60_000_000  # a 60-megapixel photo is already more than OCR needs


def _check_image(name: str, data: bytes) -> None:
    """Reads only the header (no decoding), so a tiny file claiming huge dimensions is refused."""
    import io

    from PIL import Image
    import warnings
    try:
        with warnings.catch_warnings():  # our own limit below applies; PIL's warning is noise here
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
    except Exception:
        raise HTTPException(415, f"{name} could not be opened as an image.")
    if w * h > MAX_IMAGE_PIXELS:
        raise HTTPException(413, f"{name} is too large an image ({w} x {h}). Take a smaller screenshot or photo.")


def _check_pdf(name: str, data: bytes) -> None:
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        raise HTTPException(415, f"{name} could not be opened. It may be damaged or password-protected.")
    try:
        n = len(pdf)
    finally:
        pdf.close()
    limit = settings()["app"]["max_pages_per_file"]
    if n == 0:
        raise HTTPException(415, f"{name} has no pages.")
    if n > limit:
        raise HTTPException(413, f"{name} has {n} pages, more than the {limit} the app reads. Add only the quote pages.")


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    cid, _, fid = job_id.rpartition(".")
    f = storage.find_file(storage.load(cid), fid)
    return {"stage": f["status"], "summary": f.get("summary"), "error": f.get("error")}


@app.get("/api/comparisons/{cid}/progress")
def progress(cid: str):
    comp = storage.load(cid)
    return {"revision": comp["revision"], "files": [present.file_view(f) for f in comp["files"]]}


@app.delete("/api/comparisons/{cid}/files/{fid}")
def remove_file(cid: str, fid: str):
    removed = {}

    def change(comp):
        f = storage.find_file(comp, fid)
        removed.update(f)
        ids = {q["id"] for q in comp["quotes"] if q["file_id"] == fid}
        comp["files"] = [x for x in comp["files"] if x["id"] != fid]
        comp["quotes"] = [q for q in comp["quotes"] if q["file_id"] != fid]
        comp["edits"] = [e for e in comp["edits"] if e["quote_id"] not in ids]
        comp["recommendation"] = {k: v for k, v in comp["recommendation"].items() if v not in ids}
        comp["current"] = {k: v for k, v in comp["current"].items() if v not in ids}
    storage.update(cid, change)
    storage.remove_file_artifacts(cid, removed)
    return view(cid)


@app.delete("/api/comparisons/{cid}/quotes/{qid}")
def remove_quote(cid: str, qid: str):
    """Remove one quote. When it is the file's only quote, the whole file goes."""
    comp = storage.load(cid)
    q = next((q for q in comp["quotes"] if q["id"] == qid), None)
    if q is None:
        raise storage.NotFound(qid)
    if sum(1 for x in comp["quotes"] if x["file_id"] == q["file_id"]) == 1:
        return remove_file(cid, q["file_id"])

    def change(c):
        c["quotes"] = [x for x in c["quotes"] if x["id"] != qid]
        c["edits"] = [e for e in c["edits"] if e["quote_id"] != qid]
        c["recommendation"] = {k: v for k, v in c["recommendation"].items() if v != qid}
        c["current"] = {k: v for k, v in c["current"].items() if v != qid}
        f = storage.find_file(c, q["file_id"])
        f["quote_ids"] = [x for x in f.get("quote_ids", []) if x != qid]
    storage.update(cid, change)
    return view(cid)


@app.post("/api/comparisons/{cid}/files/{fid}/retry")
def retry(cid: str, fid: str):
    def change(comp):
        f = storage.find_file(comp, fid)
        if f["status"] in storage.ACTIVE:
            return
        f.update(status="queued", job_kind="recheck", error=None)
    storage.update(cid, change)
    worker.enqueue(cid, fid, "recheck")
    return view(cid)


@app.post("/api/comparisons/{cid}/quotes/{qid}/move")
def move_quote(cid: str, qid: str, body: MoveQuote):
    """The broker says this quote is for another line: read the file again with that correction."""
    from app.config import lines_config
    if body.line not in set(lines_config()["lines"]) | {"unknown"}:
        raise HTTPException(422, "Unknown line of business.")
    comp = storage.load(cid)
    q = next((q for q in comp["quotes"] if q["id"] == qid), None)
    if q is None:
        raise storage.NotFound(qid)
    if q["line"] == body.line:
        return view(cid)

    def change(c):
        f = storage.find_file(c, q["file_id"])
        if f["status"] in storage.ACTIVE:
            raise HTTPException(409, "This file is still being read. Try again when it is done.")
        hints = [h for h in f.get("line_hints", []) if not (h["carrier"] == q.get("carrier")
                                                            and h.get("price") == (q.get("price") or {}).get("value"))]
        hints.append({"carrier": q.get("carrier"), "price": (q.get("price") or {}).get("value"), "line": body.line})
        f.update(line_hints=hints, status="queued", job_kind="move", error=None)
    storage.update(cid, change)
    worker.enqueue(cid, q["file_id"], "move")
    return view(cid)


# ---------------------------------------------------------------- cells

@app.patch("/api/comparisons/{cid}/cells")
def change_cell(cid: str, body: CellChange):
    def change(comp):
        if body.quote_id not in {q["id"] for q in comp["quotes"]}:
            raise storage.NotFound(body.quote_id)
        target = body.model_dump()
        if body.key == "term" and not (body.action == "undo" or (body.action == "edit" and body.value in ("6", "12"))):
            raise HTTPException(422, "The policy term is 6 or 12 months.")
        if body.action == "undo":  # every edit of this value, including the one that added it
            comp["edits"] = [e for e in comp["edits"] if not present.same_target(e, target)]
            return
        value = (body.value or "").strip()
        if body.action == "edit" and not value:
            if body.index is None and body.key != "price":
                return  # nothing typed into an empty cell
            raise HTTPException(422, "Type a value, or use Remove to take it out.")
        if body.action == "clear" and body.key == "price":
            raise HTTPException(422, "The price can be changed but not removed.")
        # "Looks right" remembers the value the broker saw, so it never approves a different value after a re-read
        old = value if body.action == "accept" and body.value is not None else None
        comp["edits"].append(present.new_edit(target, body.action, value if body.action == "edit" else None, old))
    storage.update(cid, change)
    return view(cid)


# ---------------------------------------------------------------- images and files

@app.get("/api/comparisons/{cid}/pages/{fid}/{n}")
def page_image(cid: str, fid: str, n: int):
    path = storage.page_png(cid, fid, n)
    if not path.exists():
        raise storage.NotFound(fid)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/comparisons/{cid}/files/{fid}/original")
def original(cid: str, fid: str):
    comp = storage.load(cid)
    f = storage.find_file(comp, fid)
    path = storage.file_path(cid, f)
    if not path.exists():
        raise storage.NotFound(fid)
    media = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}[f["ext"]]
    return FileResponse(path, media_type=media, filename=f["name"], content_disposition_type="inline",
                        headers={"Content-Security-Policy": "frame-ancestors 'none'"})


# ---------------------------------------------------------------- export

@app.get("/api/comparisons/{cid}/export.pdf")
def export_pdf(cid: str):
    from app import export_pdf as ex
    comp = storage.load(cid)
    pdf = ex.render(comp, present.view(comp))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{ex.file_name(comp)}"'})


@app.get("/api/comparisons/{cid}/export-preview.png")
def export_preview(cid: str):
    return export_preview_page(cid, 1)


@app.get("/api/comparisons/{cid}/export-preview")
def export_preview_info(cid: str):
    """How many pages the client PDF has right now (the proposal preview then loads each one)."""
    from app import export_pdf as ex
    comp = storage.load(cid)
    return {"revision": comp["revision"], "pages": len(ex.preview_pages(comp, present.view(comp)))}


@app.get("/api/comparisons/{cid}/export-preview/{n}.png")
def export_preview_page(cid: str, n: int):
    from app import export_pdf as ex
    comp = storage.load(cid)
    pages = ex.preview_pages(comp, present.view(comp))
    if not 1 <= n <= len(pages):
        raise storage.NotFound(str(n))
    return Response(pages[n - 1], media_type="image/png", headers={"Cache-Control": "no-store"})


def main() -> None:
    import uvicorn
    cfg = settings()["app"]
    host = cfg["host"]
    if host not in ALLOWED_HOSTS:
        raise SystemExit(f"app.host must be 127.0.0.1 or localhost, not {host}")
    uvicorn.run(app, host=host, port=int(cfg["port"]), log_level="warning")


if __name__ == "__main__":
    main()
