"""Step 1: Read. PDF (or image) -> pages with text and source ("text" layer or "ocr").

Per page: use the pypdfium2 text layer when it has enough characters and no big image
covers the page; otherwise render at 300 DPI and OCR (dedot + deskew + Tesseract).

    python -m app.read fixtures/*.pdf
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
import pytesseract

from app.config import DATA_DIR, settings

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


# ---------------------------------------------------------------- image prep

def dedot(gray: np.ndarray) -> tuple[np.ndarray, int]:
    """Remove dotted leader lines: rows of many tiny, evenly spaced blobs.

    Same algorithm and thresholds as reference/dedot.py, working on an array in memory.
    """
    bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n, _, st, cen = cv2.connectedComponentsWithStats(bw, 8)
    small = [i for i in range(1, n) if st[i, 4] < 40 and st[i, 2] <= 8 and st[i, 3] <= 8]
    # group small blobs by y (4px buckets); a row with >=15 of them is a leader line
    ys: dict[int, list[int]] = {}
    for i in small:
        ys.setdefault(int(cen[i][1]) // 4, []).append(i)
    kill = set()
    for ids in ys.values():
        if len(ids) >= 15:
            kill.update(ids)
    clean = gray.copy()
    for i in kill:
        x, y, w, h, _ = st[i]
        clean[y:y + h, x:x + w] = 255
    return clean, len(kill)


def _rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def find_skew(gray: np.ndarray) -> float:
    """Angle (degrees) that makes text rows line up best.

    Tries -5..+5 in 0.25 steps on a downscaled copy and picks the angle whose row sums of
    dark pixels are most uneven (highest variance).
    """
    scale = 1000 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-5, 5.0001, 0.25):
        rows = _rotate_binary(bw, angle).sum(axis=1, dtype=np.float64)
        score = float(np.var(rows))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle


def _rotate_binary(bw: np.ndarray, angle: float) -> np.ndarray:
    h, w = bw.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)


def deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    angle = find_skew(gray)
    if abs(angle) > 0.3:
        return _rotate(gray, angle), angle
    return gray, angle


def prepare_for_ocr(gray: np.ndarray) -> tuple[np.ndarray, dict]:
    """Deskew, then remove dotted leaders.

    Deskew runs first: dedot groups dots into 4px-high rows, so on a page tilted by even
    1 degree a long leader line spreads over many rows and is no longer detected.
    """
    straight, angle = deskew(gray)
    clean, dots = dedot(straight)
    return clean, {"skew": round(angle, 2), "dots_removed": dots}


# Where the Windows installers put Tesseract (it is often not on PATH there).
TESSERACT_WINDOWS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]


def tesseract_cmd() -> str | None:
    """The Tesseract program: settings.yaml `ocr.tesseract_cmd`, else PATH, else the usual Windows folders."""
    import os
    import shutil
    configured = settings()["ocr"].get("tesseract_cmd")
    if configured:
        return configured if os.path.exists(configured) else None
    found = shutil.which("tesseract")
    if found:
        return found
    local = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")
    homebrew = ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]  # a Mac app started outside Terminal
    return next((p for p in [*TESSERACT_WINDOWS, local, *homebrew] if os.path.exists(p)), None)


def tesseract(gray: np.ndarray, psm: int | None = None, layout: bool | None = None) -> str:
    cfg = settings()
    cmd = tesseract_cmd()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    psm = psm or cfg["ocr"]["tesseract_psm"]
    layout = cfg["ocr"].get("layout", False) if layout is None else layout
    if layout:
        return tesseract_layout(gray, psm)
    return pytesseract.image_to_string(gray, config=f"--oem 1 --psm {psm}")


COLUMN_GAP_CHARS = 3  # a gap this many characters wide or more separates table columns


def tesseract_layout(gray: np.ndarray, psm: int) -> str:
    """OCR text with each word placed at the character column matching its x position.

    Plain OCR collapses the gap between table columns, so a lone "Included" could be a limit
    or a premium. Placing words by position keeps columns aligned, like `pdftotext -layout`.
    """
    d = pytesseract.image_to_data(gray, config=f"--oem 1 --psm {psm}", output_type=pytesseract.Output.DICT)
    words = [i for i, t in enumerate(d["text"]) if t.strip()]
    if not words:
        return ""
    widths = [d["width"][i] / len(d["text"][i]) for i in words if len(d["text"][i]) >= 3]
    char_w = statistics.median(widths) if widths else 20.0
    lines: dict[tuple, list[int]] = {}
    for i in words:
        lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(i)
    out = []
    for ids in lines.values():  # dicts keep Tesseract's reading order
        s = ""
        prev_right = None
        for i in sorted(ids, key=lambda i: d["left"][i]):
            col = round(d["left"][i] / char_w)
            gap = (d["left"][i] - prev_right) / char_w if prev_right is not None else col
            # only a wide gap marks a new column; a normal word gap is one space
            if s and gap < COLUMN_GAP_CHARS:
                s += " "
            else:
                s += " " * (col - len(s)) if col > len(s) else (" " if s else "")
            s += d["text"][i]
            prev_right = d["left"][i] + d["width"][i]
        out.append(s.rstrip())
    margin = min(len(ln) - len(ln.lstrip()) for ln in out if ln.strip())
    return "\n".join(ln[margin:] for ln in out)


# ---------------------------------------------------------------- PDF pages

def _pil_to_gray(pil) -> np.ndarray:
    return np.array(pil.convert("L"))


MAX_RENDER_SIDE = 7000  # pixels; a letter page at 300 DPI is 3300. Stops an odd huge page from exhausting memory.


def safe_scale(page, dpi: int, max_side: int = MAX_RENDER_SIDE) -> float:
    w, h = page.get_size()  # points
    return min(dpi / 72, max_side / max(w, h, 1))


def render(page, dpi: int) -> np.ndarray:
    return _pil_to_gray(page.render(scale=safe_scale(page, dpi)).to_pil())


def image_boxes(page) -> list[tuple[float, float, float, float]]:
    """(left, bottom, right, top) of every image on the page, in PDF points."""
    return [obj.get_bounds() for obj in page.get_objects(max_depth=15)
            if obj.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE]


def ocr_images_on_text_page(page, dpi: int) -> str:
    """Text inside images (e.g. a carrier's logo) that the text layer does not contain."""
    boxes = [b for b in image_boxes(page) if (b[2] - b[0]) > 20 and (b[3] - b[1]) > 8]
    if not boxes:
        return ""
    _, page_h = page.get_size()
    gray = render(page, dpi)
    scale = dpi / 72
    found = []
    for left, bottom, right, top in boxes:
        x0, y0 = max(int(left * scale) - 4, 0), max(int((page_h - top) * scale) - 4, 0)
        x1, y1 = int(right * scale) + 4, int((page_h - bottom) * scale) + 4
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0 or crop.min() > 200:  # blank or very light image
            continue
        text = " ".join(pytesseract.image_to_string(crop, config="--oem 1 --psm 6").split())
        if sum(c.isalpha() for c in text) >= 3:
            found.append(text)
    return "\n".join(found)


def max_image_fraction(page) -> float:
    w, h = page.get_size()
    best = 0.0
    for obj in page.get_objects(max_depth=15):
        if obj.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE:
            left, bottom, right, top = obj.get_bounds()
            best = max(best, (right - left) * (top - bottom) / (w * h))
    return best


def visible_text(page) -> tuple[str, int]:
    """The page's text layer without text drawn invisibly, and how many characters were dropped.

    Invisible text is never seen by the broker but would pass the value checks: a scanner's own
    OCR layer (with its misreads) or planted instructions. A page with none is read exactly as before."""
    textpage = page.get_textpage()
    if not any(obj.type == pdfium_raw.FPDF_PAGEOBJ_TEXT and
               pdfium_raw.FPDFTextObj_GetTextRenderMode(obj.raw) == pdfium_raw.FPDF_TEXTRENDERMODE_INVISIBLE
               for obj in page.get_objects(max_depth=15)):
        return textpage.get_text_range(), 0
    units, dropped = [], 0
    for i in range(pdfium_raw.FPDFText_CountChars(textpage.raw)):
        obj = pdfium_raw.FPDFText_GetTextObject(textpage.raw, i)
        if obj and pdfium_raw.FPDFTextObj_GetTextRenderMode(obj) == pdfium_raw.FPDF_TEXTRENDERMODE_INVISIBLE:
            dropped += 1
            continue
        u = pdfium_raw.FPDFText_GetUnicode(textpage.raw, i)
        if u:
            units.append(u)
    text = "".join(map(chr, units)).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text, dropped


def _clean_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def read_pdf_page(page, cfg: dict) -> dict:
    t0 = time.perf_counter()
    raw_text, hidden = visible_text(page)
    text = _clean_text(raw_text)
    use_layer = (len(text) >= cfg["reader"]["min_text_layer_chars"]
                 and max_image_fraction(page) <= cfg["reader"]["max_image_page_fraction"])
    if use_layer:
        prep = {"hidden_chars_dropped": hidden} if hidden else {}
        if cfg["reader"].get("ocr_images_on_text_pages"):
            image_text = ocr_images_on_text_page(page, cfg["ocr"]["dpi"])
            if image_text:
                text += "\n[text inside images on this page]\n" + image_text
                prep["image_text_lines"] = image_text.count("\n") + 1
        return {"text": text, "source": "text", "seconds": time.perf_counter() - t0, "prep": prep}
    gray = render(page, cfg["ocr"]["dpi"])
    clean, prep = prepare_for_ocr(gray)
    text = _clean_text(tesseract(clean))
    return {"text": text, "source": "ocr", "seconds": time.perf_counter() - t0, "prep": prep}


def read_file(path: str | Path, pages_dir: Path | None = None) -> list[dict]:
    """Read one PDF or image. Returns [{page, text, source, seconds, prep, png}]."""
    path = Path(path)
    cfg = settings()
    pages_dir = pages_dir or DATA_DIR / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    out = []

    if path.suffix.lower() in IMAGE_SUFFIXES:
        t0 = time.perf_counter()
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise ValueError("image could not be decoded")
        side = max(gray.shape)
        if side > 5000:  # a large phone photo: OCR is as good at 5000 px and much faster
            gray = cv2.resize(gray, None, fx=5000 / side, fy=5000 / side, interpolation=cv2.INTER_AREA)
        clean, prep = prepare_for_ocr(gray)
        text = _clean_text(tesseract(clean))
        png = pages_dir / f"{path.stem}_p1.png"
        scale = cfg["ocr"]["preview_dpi"] / cfg["ocr"]["dpi"]
        cv2.imwrite(str(png), cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA))
        return [{"page": 1, "text": text, "source": "ocr", "seconds": time.perf_counter() - t0,
                 "prep": prep, "png": str(png)}]

    pdf = pdfium.PdfDocument(str(path))
    try:
        for i, page in enumerate(pdf, start=1):
            result = read_pdf_page(page, cfg)
            png = pages_dir / f"{path.stem}_p{i}.png"
            page.render(scale=safe_scale(page, cfg["ocr"]["preview_dpi"], 2400)).to_pil().save(png)
            out.append({"page": i, **result, "png": str(png)})
            page.close()
    finally:
        pdf.close()
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Read PDFs/images into page text.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--show-text", action="store_true", help="print each page's text")
    args = ap.parse_args(argv)
    for f in args.files:
        for p in read_file(f):
            print(f"{Path(f).name} p{p['page']}: {p['source']:4} {len(p['text']):5} chars "
                  f"{p['seconds']:.2f}s {p['prep']}")
            if args.show_text:
                print(p["text"], "\n" + "-" * 60)


if __name__ == "__main__":
    main(sys.argv[1:])
