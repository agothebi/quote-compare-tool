"""The client's PDF (reportlab). Portrait letter, laid out like the proposal sheet in mockup-v5:
the agency's navy header band, "Your insurance options, compared", brown section bars.

Order: "Our recommendation" (a card per policy the broker picked: carrier, yearly price, the reasons
he ticked or typed), the household total ("Everything above, together"), one "<Policy> compared"
table per line of business with a Good to know line under it, then the broker's "Note from us".
Left out on purpose: the hidden "Other coverages" section, every internal state (check marks,
"needs a check", edited), and broker-only notes (no pay-in-full price, totals problems). Values are
printed exactly as the broker's table shows them.

The broker's proposal options (comparison.json "export") choose: the full comparison or the
recommendation only, the household total, a plain-words line under each coverage, and Good to know.
"""
from __future__ import annotations

import io
import re
import threading
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)
from reportlab.pdfgen import canvas as rl_canvas

from app.config import ROOT, settings

ASSETS = ROOT / "app" / "assets"
NAVY = colors.HexColor("#2d3a4a")
BROWN = colors.HexColor("#8b6347")
INK = colors.HexColor("#1d2530")
GREY = colors.HexColor("#5d646d")
BULLET_INK = colors.HexColor("#3f4a55")
GLOSS = colors.HexColor("#8a929b")
FOOT = colors.HexColor("#979ca3")
HEAD_SUB = colors.HexColor("#c9d1dc")
CARD_LINE = colors.HexColor("#e5e1d9")
ROW_LINE = colors.HexColor("#ebe7e0")
REC_BG = colors.HexColor("#f7f1eb")
TOTAL_BG = colors.HexColor("#f3ebe3")
NOTE_BG = colors.HexColor("#faf8f5")
PAGE = letter
MARGIN = 32
FIRST_HEAD = 60   # the header band on page 1
LATER_HEAD = 26   # a slim band on the pages after it
FOOTER = 26
MAX_COLS = 4      # quotes side by side in one table; more split into another table
CARDS_PER_ROW = 4
# Good-to-know sentences the client sees. The rest are for the broker only.
CLIENT_NOTICES = {"estimated_premium", "quote_expiration", "uninsured_motorist_rejected", "below_umbrella_requirement",
                  "underlying_auto_bi", "underlying_auto_pd", "underlying_home_liability"}

_fonts_lock = threading.Lock()
_fonts_ready = False


def _fonts() -> None:
    global _fonts_ready
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    with _fonts_lock:
        if _fonts_ready:
            return
        for weight, name in ((400, "Plex"), (500, "Plex-Medium"), (600, "Plex-SemiBold"), (700, "Plex-Bold")):
            pdfmetrics.registerFont(TTFont(name, str(ASSETS / "fonts" / f"IBMPlexSans-{weight}.ttf")))
        pdfmetrics.registerFontFamily("Plex", normal="Plex", bold="Plex-SemiBold", italic="Plex", boldItalic="Plex-SemiBold")
        _fonts_ready = True


def _styles() -> dict:
    base = dict(fontName="Plex", textColor=INK, alignment=TA_LEFT)
    semi = {**base, "fontName": "Plex-SemiBold"}
    return {
        "title": ParagraphStyle("title", fontSize=14, leading=18, **{**semi, "textColor": NAVY}),
        "bar": ParagraphStyle("bar", fontSize=7.8, leading=10, **{**semi, "textColor": colors.white}),
        "body": ParagraphStyle("body", fontSize=9, leading=13, **base),
        "card_ln": ParagraphStyle("card_ln", fontSize=7, leading=9, **{**semi, "textColor": BROWN}),
        "card_c": ParagraphStyle("card_c", fontSize=10, leading=13, **semi),
        "card_p": ParagraphStyle("card_p", fontSize=10.5, leading=14, **{**semi, "textColor": NAVY}),
        "bullet": ParagraphStyle("bullet", fontSize=7.8, leading=10.4, leftIndent=8, bulletIndent=0,
                                 **{**base, "textColor": BULLET_INK}),
        "total_l": ParagraphStyle("total_l", fontSize=9, leading=12, **base),
        "total_r": ParagraphStyle("total_r", fontSize=9, leading=14, **{**base, "alignment": TA_RIGHT}),
        "small": ParagraphStyle("small", fontSize=7.2, leading=9.4, **{**base, "textColor": GREY}),
        "th": ParagraphStyle("th", fontSize=8, leading=10.4, **{**semi, "textColor": NAVY}),
        "td": ParagraphStyle("td", fontSize=8, leading=10.4, **base),
        "label": ParagraphStyle("label", fontSize=8, leading=10.4, **{**base, "textColor": GREY}),
        "gloss": ParagraphStyle("gloss", fontSize=6.8, leading=8.6, **{**base, "textColor": GLOSS}),
        "gtk": ParagraphStyle("gtk", fontSize=7.8, leading=10.6, **{**base, "textColor": GREY}),
        "note": ParagraphStyle("note", fontSize=9, leading=13, **base),
    }


def _p(text: str, style) -> Paragraph:
    return Paragraph(escape(text or "").replace("\n", "<br/>"), style)


def _hex(c) -> str:
    return "#" + c.hexval()[2:]


def _date() -> str:
    d = datetime.now()
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def file_name(comp: dict) -> str:
    import unicodedata  # "José Núñez" -> "Jose_Nunez", not "Jos_N_ez"
    ascii_name = unicodedata.normalize("NFKD", comp["client"]["name"]).encode("ascii", "ignore").decode()
    safe = re.sub(r"[^A-Za-z0-9]+", "_", ascii_name).strip("_")[:60] or "Client"
    return f"{safe}_Quote_Comparison_{datetime.now().strftime('%Y-%m-%d')}.pdf"


def _options(comp: dict) -> dict:
    from app.storage import EXPORT_DEFAULTS
    return {**EXPORT_DEFAULTS, **(comp.get("export") or {})}


# ---------------------------------------------------------------- page furniture

class _NumberedCanvas(rl_canvas.Canvas):
    """Draws 'Agency · Page N of M' once the page count is known."""

    def __init__(self, *args, agency_name: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []
        self._agency = agency_name

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Plex", 6.5)
            self.setFillColor(FOOT)
            self.drawRightString(PAGE[0] - MARGIN, FOOTER - 13, f"{self._agency} · Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _fit(c, text: str, font: str, size: float, width: float) -> str:
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _footer(c, agency: dict) -> None:
    w = PAGE[0]
    c.setStrokeColor(CARD_LINE)
    c.setLineWidth(0.6)
    c.line(MARGIN, FOOTER, w - MARGIN, FOOTER)
    c.setFont("Plex", 6.5)
    c.setFillColor(FOOT)
    c.drawString(MARGIN, FOOTER - 13, _fit(c, agency["disclaimer"], "Plex", 6.5, w * 0.62))


def _first_page(comp: dict, agency: dict):
    client = comp["client"]

    def draw(c: rl_canvas.Canvas, doc) -> None:
        w, h = PAGE
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - FIRST_HEAD, w, FIRST_HEAD, stroke=0, fill=1)
        tile = 34
        ty = h - FIRST_HEAD + (FIRST_HEAD - tile) / 2
        c.setFillColor(colors.white)
        c.roundRect(MARGIN, ty, tile, tile, 5, stroke=0, fill=1)
        logo = ASSETS / "logo.png"
        if logo.exists():
            c.drawImage(str(logo), MARGIN + 3, ty + 3, tile - 6, tile - 6, preserveAspectRatio=True, anchor="c", mask="auto")
        x = MARGIN + tile + 10
        c.setFillColor(colors.white)
        c.setFont("Plex-SemiBold", 11)
        c.drawString(x, h - FIRST_HEAD / 2 + 2, agency["name"])
        c.setFont("Plex", 7.5)
        c.setFillColor(HEAD_SUB)
        c.drawString(x, h - FIRST_HEAD / 2 - 10, _fit(c, f"{agency['address']} · {agency['phone']}", "Plex", 7.5, w * 0.45))
        right = w - MARGIN
        lines = [x for x in (client.get("address"), _date()) if x]
        top = h - FIRST_HEAD / 2 + (6 if len(lines) == 1 else 11)
        c.setFillColor(colors.white)
        c.setFont("Plex-SemiBold", 9.5)
        c.drawRightString(right, top, _fit(c, client["name"], "Plex-SemiBold", 9.5, w * 0.4))
        c.setFont("Plex", 7.5)
        c.setFillColor(HEAD_SUB)
        for i, line in enumerate(lines):
            c.drawRightString(right, top - 11 * (i + 1), _fit(c, line, "Plex", 7.5, w * 0.4))
        _footer(c, agency)
        c.restoreState()
    return draw


def _later_page(comp: dict, agency: dict):
    def draw(c: rl_canvas.Canvas, doc) -> None:
        w, h = PAGE
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - LATER_HEAD, w, LATER_HEAD, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Plex-SemiBold", 8.5)
        c.drawString(MARGIN, h - LATER_HEAD / 2 - 3, agency["name"])
        c.setFont("Plex", 7.5)
        c.setFillColor(HEAD_SUB)
        c.drawRightString(w - MARGIN, h - LATER_HEAD / 2 - 3,
                          _fit(c, f"{comp['client']['name']} · Your insurance options, compared", "Plex", 7.5, w * 0.6))
        _footer(c, agency)
        c.restoreState()
    return draw


# ---------------------------------------------------------------- pieces

def _bar(text: str, st: dict, width: float) -> Table:
    t = Table([[_p(text.upper(), st["bar"])]], colWidths=[width], hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BROWN), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5)]))
    return t


def _notices(tab: dict, i: int) -> list[dict]:
    row = next((r for r in tab["rows"] if r["section"] == "good_to_know"), None)
    return (row["cells"][i].get("notices") or []) if row else []


def _is_estimate(tab: dict, i: int) -> bool:
    return any(n.get("key") == "estimated_premium" for n in _notices(tab, i))


def _price(tab: dict, i: int) -> tuple[str, str, str]:
    """(amount, unit, extra) for a quote: ('≈ $2,599.42', ' / year', ' ($1,299.71 per 6 months)').
    From the view's price summary; a view without one (tests) falls back to the printed price."""
    prices = tab.get("prices")
    price_row = next((r for r in tab["rows"] if r["section"] == "price"), None)
    cell = price_row["cells"][i] if price_row else {"lines": ["—"]}
    if not prices:
        return (cell["lines"][0] if cell.get("lines") else "—"), "", ""
    p = prices[i]
    if not p.get("amount"):
        return "—", "", ""
    amount = ("≈ " if p.get("estimated") else "") + p["amount"]
    unit = {"year": " / year", "month": " / month"}.get(p.get("unit"), "")
    extra = f" ({p['printed']} per 6 months)" if p.get("six_month") and p.get("printed") else ""
    return amount, unit, extra


def _price_cell(tab: dict, i: int) -> str:
    """The price in the compared table: '≈ $2,599.42' / '($1,299.71 per 6 months)' / ' (estimate)'."""
    amount, unit, extra = _price(tab, i)
    if amount == "—":
        return "—"
    text = amount + (" a month" if unit == " / month" else "")
    if extra:
        text += "\n" + extra.strip()
    if _is_estimate(tab, i):
        text += " (estimate)"
    return text


def _cards(view: dict, st: dict, width: float) -> Table | None:
    """One card per policy with a pick: the policy, the carrier, the yearly price, the reasons."""
    cards = []
    for tab in view["tabs"]:
        rec = tab.get("recommended")
        if not rec:
            continue
        i = next(i for i, c in enumerate(tab["columns"]) if c["quote_id"] == rec)
        amount, unit, extra = _price(tab, i)
        tail = unit + extra + (", estimate" if _is_estimate(tab, i) else "")
        flow = [_p(tab["label"].upper(), st["card_ln"]), Spacer(1, 1), _p(tab["columns"][i]["carrier"], st["card_c"]),
                Paragraph(f'{escape(amount)}<font name="Plex" size="7.2" color="{_hex(GREY)}">{escape(tail)}</font>', st["card_p"])]
        reasons = [r for r in tab.get("reasons", []) if isinstance(r, str) and r.strip()]
        if reasons:
            flow.append(Spacer(1, 3))
            flow += [Paragraph(escape(r), st["bullet"], bulletText="•") for r in reasons]
        cards.append(flow)
    if not cards:
        return None
    n = min(len(cards), CARDS_PER_ROW)
    gap = 8
    card_w = (width - gap * (n - 1)) / n
    data, style = [], [("VALIGN", (0, 0), (-1, -1), "TOP")]
    for r0 in range(0, len(cards), n):
        if data:
            data.append([""] * (2 * n - 1))  # the space between rows of cards
        row = []
        for k in range(n):
            if k:
                row.append("")
            row.append(cards[r0 + k] if r0 + k < len(cards) else "")
        data.append(row)
        ri = len(data) - 1
        for k in range(n):
            if r0 + k >= len(cards):
                continue
            c = 2 * k
            style += [("BOX", (c, ri), (c, ri), 0.6, CARD_LINE), ("LINEABOVE", (c, ri), (c, ri), 2.2, BROWN),
                      ("LEFTPADDING", (c, ri), (c, ri), 8), ("RIGHTPADDING", (c, ri), (c, ri), 8),
                      ("TOPPADDING", (c, ri), (c, ri), 7), ("BOTTOMPADDING", (c, ri), (c, ri), 8)]
    widths = [w for k in range(n) for w in ([gap, card_w] if k else [card_w])]
    heights = [gap if r % 2 else None for r in range(len(data))]
    t = Table(data, colWidths=widths, rowHeights=heights, hAlign="LEFT")
    t.setStyle(TableStyle(style))
    return t


def _total(view: dict, st: dict, width: float) -> Table | None:
    total = view.get("recommended_total")
    if not total:
        return None
    est = total.get("estimated")
    right = (f'<font name="Plex-SemiBold" size="11.5" color="{_hex(NAVY)}">{"≈ " if est else ""}{escape(total["amount"])}</font>'
             f' a year{" (estimated)" if est else ""}')
    if total.get("per_month"):
        right += f" · about {escape(total['per_month'])} a month"
    rows = [[_p("Everything above, together", st["total_l"]), Paragraph(right, st["total_r"])]]
    if total.get("left_out"):
        rows.append([_p(f"Does not include {', '.join(x.lower() for x in total['left_out'])} (priced monthly).", st["small"]), ""])
    t = Table(rows, colWidths=[width * 0.4, width * 0.6], hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TOTAL_BG), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def _cell_text(cell: dict) -> str:
    if cell["state"] == "not_listed":
        return "Not listed"
    text = "\n".join(cell["lines"])
    if cell.get("note"):
        text += f"\n({cell['note']})"
    return text


def _table(tab: dict, cols: list[int], st: dict, width: float, opts: dict) -> Table:
    rec = tab.get("recommended")
    columns = [tab["columns"][i] for i in cols]
    data = [[""] + [_p((c["carrier"] or "Unknown carrier") + (" · Recommended" if c["quote_id"] == rec else ""), st["th"])
                    for c in columns]]
    units = {p.get("unit") for p in tab.get("prices") or [] if p.get("unit")}
    price_label = "Premium" if tab["line"] == "life" else "Price" if "month" in units else "Price per year"
    data.append([_p(price_label, st["label"])] + [_p(_price_cell(tab, i), st["td"]) for i in cols])
    for r in tab["rows"]:
        if r["section"] not in ("vehicles", "core", "also"):
            continue
        cells = [r["cells"][i] for i in cols]
        if all(c["state"] == "not_listed" for c in cells):
            continue  # a row none of these quotes lists
        label = [_p(r["label"], st["label"])]
        if opts.get("gloss") and r.get("help"):
            label.append(_p(r["help"], st["gloss"]))
        data.append([label] + [_p(_cell_text(c), st["td"] if c["state"] != "not_listed" else st["label"]) for c in cells])
    label_w = width * 0.3
    col_w = (width - label_w) / max(1, len(columns))
    # splitInRow: a cell taller than a page (dozens of scheduled items) continues on the next page
    t = Table(data, colWidths=[label_w] + [col_w] * len(columns), repeatRows=1, hAlign="LEFT", splitInRow=1)
    ts = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
          ("LINEBELOW", (0, 0), (-1, -1), 0.5, ROW_LINE)]
    for j, c in enumerate(columns, start=1):
        if c["quote_id"] == rec:
            ts.append(("BACKGROUND", (j, 0), (j, -1), REC_BG))
    t.setStyle(TableStyle(ts))
    return t


def _good_to_know(tab: dict, cols: list[int], st: dict) -> Paragraph | None:
    parts = []
    for i in cols:  # one mention of each carrier, its sentences after it
        carrier = tab["columns"][i]["carrier"] or "Unknown carrier"
        texts = [escape(n["text"]) for n in _notices(tab, i) if n.get("key") in CLIENT_NOTICES and n.get("text")]
        if texts:
            parts.append(f"{escape(carrier)}: {' '.join(texts)}")
    if not parts:
        return None
    return Paragraph(f'<font name="Plex-SemiBold" color="{_hex(NAVY)}">Good to know.</font> ' + " ".join(parts), st["gtk"])


def _note(notes: str, st: dict, width: float) -> Table:
    # one table row per paragraph (not one cell), so notes of any length flow across pages
    paras = [x.strip() for x in re.split(r"\n\s*\n|\n", notes) if x.strip()]
    t = Table([[_p(x, st["note"])] for x in paras], colWidths=[width], hAlign="LEFT", splitInRow=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NOTE_BG), ("LINEBEFORE", (0, 0), (0, -1), 2.2, BROWN),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 7)]))
    return t


# ---------------------------------------------------------------- the document

def render(comp: dict, view: dict) -> bytes:
    _fonts()
    st = _styles()
    agency = settings()["agency"]
    opts = _options(comp)
    buf = io.BytesIO()
    w, h = PAGE
    width = w - 2 * MARGIN
    bottom = FOOTER + 14
    doc = BaseDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=FIRST_HEAD + 20, bottomMargin=bottom,
                          title=f"Insurance Quote Comparison for {comp['client']['name']}",
                          author=agency["name"], subject="Insurance quote comparison", creator=agency["name"])
    first = Frame(MARGIN, bottom, width, h - FIRST_HEAD - 20 - bottom, id="first",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    later = Frame(MARGIN, bottom, width, h - LATER_HEAD - 18 - bottom, id="later",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="first", frames=[first], onPage=_first_page(comp, agency), autoNextPageTemplate="later"),
                          PageTemplate(id="later", frames=[later], onPage=_later_page(comp, agency))])

    story = [_p("Your insurance options, compared", st["title"]), Spacer(1, 12)]
    cards = _cards(view, st, width)
    if cards:
        block = [_bar("Our recommendation", st, width), Spacer(1, 7), cards]
        total = _total(view, st, width) if opts["total"] else None
        if total:
            block += [Spacer(1, 8), total]
        story += [KeepTogether(block), Spacer(1, 16)]
    elif opts["length"] == "short" and view["tabs"]:
        story += [_p("No recommendation has been picked yet.", st["body"]), Spacer(1, 12)]
    for tab in view["tabs"] if opts["length"] == "full" else []:
        idx = list(range(len(tab["columns"])))
        chunks = [idx[i:i + MAX_COLS] for i in range(0, len(idx), MAX_COLS)] or [[]]
        for n, cols in enumerate(chunks):
            title = f"{tab['label']} compared" if tab["line"] != "unknown" else "Other policies compared"
            if len(chunks) > 1:
                title += f" ({n + 1} of {len(chunks)})"
            # a section starts on a new page only when less than a bar and a few rows would fit;
            # long tables then continue on the next page with their column headings repeated
            story += [CondPageBreak(130), _bar(title, st, width), Spacer(1, 3), _table(tab, cols, st, width, opts)]
            gtk = _good_to_know(tab, cols, st) if opts["gtk"] else None
            if gtk:
                story += [Spacer(1, 6), gtk]
            story.append(Spacer(1, 16))
    notes = (comp.get("notes") or "").strip()
    if notes:
        story += [CondPageBreak(80), _bar("Note from us", st, width), Spacer(1, 7), _note(notes, st, width)]
    if len(story) == 2 and not view["tabs"]:
        story.append(_p("No quotes have been added yet.", st["body"]))
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(*a, agency_name=agency["name"], **k))
    return buf.getvalue()


_preview_cache: dict[tuple, list[bytes]] = {}
_preview_lock = threading.Lock()
PREVIEW_SCALE = 1.5


def preview_pages(comp: dict, view: dict) -> list[bytes]:
    """Every page of the client PDF as a PNG, for the proposal preview: the real document, not a
    mock-up. Cached per comparison revision (the page count and the images come from one render)."""
    import pypdfium2 as pdfium
    key = (comp["id"], comp["revision"], _date())
    with _preview_lock:  # one render at a time; a second request for the same revision waits and reuses it
        if key in _preview_cache:
            return _preview_cache[key]
        pdf = pdfium.PdfDocument(render(comp, view))
        pngs = []
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                img = page.render(scale=PREVIEW_SCALE).to_pil()
                page.close()
                out = io.BytesIO()
                img.save(out, format="PNG", compress_level=3)
                pngs.append(out.getvalue())
        finally:
            pdf.close()
        for k in [k for k in _preview_cache if k[0] == comp["id"]]:  # older revisions of this comparison
            del _preview_cache[k]
        if len(_preview_cache) >= 8:
            _preview_cache.pop(next(iter(_preview_cache)))
        _preview_cache[key] = pngs
        return pngs


def preview_png(comp: dict, view: dict) -> bytes:
    """The PDF's first page as a PNG."""
    return preview_pages(comp, view)[0]
