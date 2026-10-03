"""The client's PDF (reportlab). Portrait letter, option C of mockups/pdf-redesign.html: a centered
letterhead (the agency's logo, its address, phone and website under it), "Prepared for" and the date
between two hairlines, the title "Insurance Quote Comparison", numbered sections (01, 02, ...),
outlined cards, striped tables. The agency's colors (blue #303540, browns #AB8D7E / #CEC0B0, greys
#555A69 / #CBCCCE); Manrope only for the title, headings, names and prices, IBM Plex for the rest.

Order: "Our recommendation" (a card per policy the broker picked: carrier, yearly price, the reasons
he ticked or typed), the household total ("Everything above, together"), one "<Policy> insurance quotes"
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
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Flowable, Frame, KeepTogether, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)
from reportlab.pdfgen import canvas as rl_canvas

from app.config import ROOT, settings

ASSETS = ROOT / "app" / "assets"
# the agency's palette
BLUE = colors.HexColor("#303540")
BROWN = colors.HexColor("#AB8D7E")    # section numbers and dots only: too light for small text
SAND = colors.HexColor("#CEC0B0")
GREY = colors.HexColor("#555A69")
SILVER = colors.HexColor("#CBCCCE")
# text and light tints of the palette
INK = colors.HexColor("#22262f")
BULLET_INK = colors.HexColor("#3d4250")
GLOSS = colors.HexColor("#8a8e99")
FOOT = colors.HexColor("#7b7f8a")
ROW_LINE = colors.HexColor("#e3e3e5")
STRIPE = colors.HexColor("#f6f6f7")
REC_BG = colors.HexColor("#f6f1ec")   # the recommended quote's whole column
NOTE_BG = colors.HexColor("#faf8f6")
PAGE = letter
MARGIN = 32
TITLE = "Insurance Quote Comparison"
FIRST_HEAD = 118  # page 1: the letterhead, down to the hairline under "Prepared for"
LATER_HEAD = 42   # the pages after it: the client on the left, the logo on the right, a hairline
LOGO_H = 54       # the logo's height on page 1
FOOTER = 26
MAX_COLS = 5      # quotes side by side in one table; more split into even tables (6 = 3 + 3)
CARDS_PER_ROW = 4 # recommendation cards in a row; more wrap evenly (5 = 3 + 2)
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
        for weight, name in ((700, "Manrope-Bold"), (800, "Manrope-ExtraBold")):  # the logo's companion face
            pdfmetrics.registerFont(TTFont(name, str(ASSETS / "fonts" / f"Manrope-{weight}.ttf")))
        pdfmetrics.registerFontFamily("Plex", normal="Plex", bold="Plex-SemiBold", italic="Plex", boldItalic="Plex-SemiBold")
        _fonts_ready = True


def _styles() -> dict:
    base = dict(fontName="Plex", textColor=INK, alignment=TA_LEFT)
    semi = {**base, "fontName": "Plex-SemiBold"}
    return {
        "title": ParagraphStyle("title", fontSize=19, leading=24, **{**base, "fontName": "Manrope-ExtraBold", "textColor": BLUE}),
        "body": ParagraphStyle("body", fontSize=9, leading=13, **base),
        "property": ParagraphStyle("property", fontSize=9, leading=13, **{**base, "textColor": BLUE}),
        "card_ln": ParagraphStyle("card_ln", fontSize=6.6, leading=9, **{**semi, "textColor": GREY}),
        "card_c": ParagraphStyle("card_c", fontSize=11, leading=14, **{**base, "fontName": "Manrope-Bold"}),
        "card_p": ParagraphStyle("card_p", fontSize=12, leading=15, **{**base, "fontName": "Manrope-ExtraBold", "textColor": BLUE}),
        "bullet": ParagraphStyle("bullet", fontSize=7.6, leading=10.2, leftIndent=8, bulletIndent=0, bulletColor=BROWN,
                                 **{**base, "textColor": BULLET_INK}),
        "total_l": ParagraphStyle("total_l", fontSize=9, leading=12, **base),
        "total_r": ParagraphStyle("total_r", fontSize=9, leading=14, **{**base, "alignment": TA_RIGHT}),
        "small": ParagraphStyle("small", fontSize=7.2, leading=9.4, **{**base, "textColor": GREY}),
        "th": ParagraphStyle("th", fontSize=8, leading=10.4, **{**semi, "textColor": BLUE}),
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
    """Draws the footer line (the disclaimer, then 'Agency · Page N of M', centered) once the page
    count is known."""

    def __init__(self, *args, agency: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []
        self._agency = agency or {}

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        w = PAGE[0]
        for state in self._saved:
            self.__dict__.update(state)
            page = f"{self._agency.get('name', '')} · Page {self._pageNumber} of {total}".lstrip(" ·")
            gap = 18
            room = w - 2 * MARGIN - self.stringWidth(page, "Plex", 6.5) - gap
            note = _fit(self, self._agency.get("disclaimer") or "", "Plex", 6.5, room)
            nw = self.stringWidth(note, "Plex", 6.5)
            x = (w - nw - (gap if note else 0) - self.stringWidth(page, "Plex", 6.5)) / 2
            self.setFont("Plex", 6.5)
            self.setFillColor(FOOT)
            if note:
                self.drawString(x, FOOTER - 13, note)
                x += nw + gap
            self.drawString(x, FOOTER - 13, page)
            super().showPage()
        super().save()


def _fit(c, text: str, font: str, size: float, width: float) -> str:
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "…" if text.strip() else ""


def _footer(c) -> None:
    c.setStrokeColor(ROW_LINE)
    c.setLineWidth(0.6)
    c.line(MARGIN, FOOTER, PAGE[0] - MARGIN, FOOTER)


def _image(c, name: str, x: float, y: float, height: float, align: str = "left") -> float:
    """Draws app/assets/<name> `height` tall, its bottom at y and its left edge, center or right edge
    at x; returns its width (0 if the file is missing)."""
    path = ASSETS / name
    if not path.exists():
        return 0
    from reportlab.lib.utils import ImageReader
    img = ImageReader(str(path))
    iw, ih = img.getSize()
    width = height * iw / ih
    left = x - width / 2 if align == "center" else x - width if align == "right" else x
    c.drawImage(img, left, y, width, height, mask="auto")
    return width


def _spaced_line(c, parts: list[str], center: float, y: float, size: float, gap: float = 9) -> None:
    """Draws parts on one centered line, with a brown dot between them."""
    dot = "·"
    widths = [c.stringWidth(p, "Plex", size) for p in parts]
    dot_w = c.stringWidth(dot, "Plex", size)
    x = center - (sum(widths) + (len(parts) - 1) * (2 * gap + dot_w)) / 2
    for i, (part, pw) in enumerate(zip(parts, widths)):
        if i:
            c.setFillColor(BROWN)
            c.drawString(x + gap, y, dot)
            x += 2 * gap + dot_w
        c.setFillColor(GREY)
        c.drawString(x, y, part)
        x += pw


def _first_page(comp: dict, agency: dict):
    client = comp["client"]

    def draw(c: rl_canvas.Canvas, doc) -> None:
        w, h = PAGE
        c.saveState()
        # the letterhead: the logo centered, the agency's address, phone and website under it
        logo_bottom = h - 22 - LOGO_H
        _image(c, "logo.png", w / 2, logo_bottom, LOGO_H, align="center")
        parts = [x for x in (agency.get("address"), agency.get("phone"), agency.get("website")) if x]
        c.setFont("Plex", 7.4)
        _spaced_line(c, [_fit(c, x, "Plex", 7.4, (w - 2 * MARGIN) / max(1, len(parts)) - 20) for x in parts],
                     w / 2, logo_bottom - 11, 7.4)
        # "Prepared for" the client (and the address), the date on the right, between two hairlines
        top, bottom = h - FIRST_HEAD + 23, h - FIRST_HEAD
        c.setStrokeColor(SILVER)
        c.setLineWidth(0.75)
        c.line(MARGIN, top, w - MARGIN, top)
        c.line(MARGIN, bottom, w - MARGIN, bottom)
        y = bottom + 8.4
        date = _date()
        c.setFont("Plex", 7.5)
        c.setFillColor(GREY)
        c.drawRightString(w - MARGIN, y, date)
        x = MARGIN
        c.setFont("Plex-SemiBold", 6.4)
        label = "PREPARED FOR"
        c.drawString(x, y + 0.6, label, charSpace=0.75)
        x += c.stringWidth(label, "Plex-SemiBold", 6.4) + 0.75 * len(label) + 8
        room = w - MARGIN - c.stringWidth(date, "Plex", 7.5) - 24 - x
        name = _fit(c, client["name"], "Manrope-Bold", 11, room)
        c.setFont("Manrope-Bold", 11)
        c.setFillColor(BLUE)
        c.drawString(x, y, name)
        x += c.stringWidth(name, "Manrope-Bold", 11)
        address = (client.get("address") or "").strip()
        if address and room - c.stringWidth(name, "Manrope-Bold", 11) > 60:
            c.setFont("Plex", 7.5)
            c.setFillColor(GREY)
            c.drawString(x, y, _fit(c, f"  ·  {address}", "Plex", 7.5, room - c.stringWidth(name, "Manrope-Bold", 11)))
        _footer(c)
        c.restoreState()
    return draw


def _later_page(comp: dict, agency: dict):
    def draw(c: rl_canvas.Canvas, doc) -> None:
        w, h = PAGE
        c.saveState()
        logo_w = _image(c, "logo.png", w - MARGIN, h - LATER_HEAD + 8, 26, align="right")
        y = h - LATER_HEAD + 17
        room = w - 2 * MARGIN - logo_w - 24
        name = _fit(c, comp["client"]["name"], "Manrope-Bold", 8.5, room * 0.6)
        c.setFont("Manrope-Bold", 8.5)
        c.setFillColor(BLUE)
        c.drawString(MARGIN, y, name)
        c.setFont("Plex", 7.5)
        c.setFillColor(GREY)
        nw = c.stringWidth(name, "Manrope-Bold", 8.5)
        c.drawString(MARGIN + nw, y, _fit(c, f"  ·  {TITLE}", "Plex", 7.5, room - nw))
        c.setStrokeColor(SILVER)
        c.setLineWidth(0.6)
        c.line(MARGIN, h - LATER_HEAD, w - MARGIN, h - LATER_HEAD)
        _footer(c)
        c.restoreState()
    return draw


# ---------------------------------------------------------------- pieces

class _Heading(Flowable):
    """A numbered section heading: '01' in brown, the title in blue, a grey hairline under both."""

    def __init__(self, number: int, text: str, width: float):
        super().__init__()
        self.number, self.text, self.width, self.height = number, text, width, 19

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        num = f"{self.number:02d}"
        c.setFont("Manrope-ExtraBold", 10.5)
        c.setFillColor(BROWN)
        c.drawString(0, 6, num)
        x = c.stringWidth(num, "Manrope-ExtraBold", 10.5) + 8
        c.setFont("Manrope-Bold", 10.5)
        c.setFillColor(BLUE)
        c.drawString(x, 6, _fit(c, self.text, "Manrope-Bold", 10.5, self.width - x))
        c.setStrokeColor(SILVER)
        c.setLineWidth(0.75)
        c.line(0, 0.4, self.width, 0.4)


def _notices(tab: dict, i: int) -> list[dict]:
    row = next((r for r in tab["rows"] if r["section"] == "good_to_know"), None)
    return (row["cells"][i].get("notices") or []) if row else []


def _is_estimate(tab: dict, i: int) -> bool:
    return any(n.get("key") == "estimated_premium" for n in _notices(tab, i))


def _estimated(tab: dict, i: int) -> bool:
    """The price shown is an estimate: a yearly figure doubled from a 6-month quote, or the carrier says so."""
    prices = tab.get("prices") or []
    return bool(i < len(prices) and prices[i].get("estimated")) or _is_estimate(tab, i)


def _price(tab: dict, i: int) -> tuple[str, str, str]:
    """(amount, unit, extra) for a quote: ('$2,599.42', ' / year', ' ($1,299.71 per 6 months)'). No "≈": the
    6-month price printed beside a doubled yearly figure says it is worked out.
    From the view's price summary; a view without one (tests) falls back to the printed price."""
    prices = tab.get("prices")
    price_row = next((r for r in tab["rows"] if r["section"] == "price"), None)
    cell = price_row["cells"][i] if price_row else {"lines": ["—"]}
    if not prices:
        return (cell["lines"][0] if cell.get("lines") else "—"), "", ""
    p = prices[i]
    if not p.get("amount"):
        return "—", "", ""
    amount = p["amount"]
    unit = {"year": " / year", "month": " / month"}.get(p.get("unit"), "")
    extra = f" ({p['printed']} per 6 months)" if p.get("six_month") and p.get("printed") else ""
    return amount, unit, extra


def _price_cell(tab: dict, i: int, style) -> Paragraph:
    """The price in the compared table: '$2,599.42 (estimated)', the word right beside the figure it
    qualifies; under it in small grey type, the 6-month price a yearly figure was doubled from."""
    amount, unit, extra = _price(tab, i)
    if amount == "—":
        return _p("—", style)
    text = escape(amount + (" a month" if unit == " / month" else ""))
    small = f'size="{style.fontSize - 0.6}" color="{_hex(GREY)}"'
    if _estimated(tab, i):
        text += f' <font {small}>(estimated)</font>'
    if extra:
        text += f'<br/><font {small}>{escape(extra.strip(" ()"))}</font>'
    return Paragraph(text, style)


def _cards(view: dict, st: dict, width: float) -> Table | None:
    """One card per policy with a pick: the policy, the carrier, the yearly price, the reasons.
    Up to four in a row; more wrap evenly (five as 3 + 2, not 4 + 1)."""
    cards = []
    for tab in view["tabs"]:
        rec = tab.get("recommended")
        if not rec:
            continue
        i = next(i for i, c in enumerate(tab["columns"]) if c["quote_id"] == rec)
        amount, unit, extra = _price(tab, i)
        tail = unit + (", estimated" if _estimated(tab, i) else "") + extra
        flow = [_p(tab["label"].upper(), st["card_ln"]), Spacer(1, 1), _p(tab["columns"][i]["carrier"], st["card_c"]),
                Paragraph(f'{escape(amount)}<font name="Plex" size="7.2" color="{_hex(GREY)}">{escape(tail)}</font>', st["card_p"])]
        reasons = [r for r in tab.get("reasons", []) if isinstance(r, str) and r.strip()]
        if reasons:
            flow.append(Spacer(1, 3))
            flow += [Paragraph(escape(r), st["bullet"], bulletText="•") for r in reasons]
        cards.append(flow)
    if not cards:
        return None
    rows = -(-len(cards) // CARDS_PER_ROW)
    n = max(2, -(-len(cards) // rows))  # a single card takes half the width, not the whole line
    gap = 8
    card_w = (width - gap * (n - 1)) / n

    def box(flow, height=None):
        t = Table([[flow]], colWidths=[card_w], rowHeights=[height] if height else None)
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.75, SILVER), ("ROUNDEDCORNERS", [5, 5, 5, 5]),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
        return t

    data = []
    for r0 in range(0, len(cards), n):
        group = cards[r0:r0 + n]
        tallest = max(box(f).wrap(card_w, 10_000)[1] for f in group)
        if data:
            data.append([""] * (2 * n - 1))  # the space between rows of cards
        row = []
        for k in range(n):
            if k:
                row.append("")
            row.append(box(group[k], tallest) if k < len(group) else "")
        data.append(row)
    widths = [w for k in range(n) for w in ([gap, card_w] if k else [card_w])]
    heights = [gap if r % 2 else None for r in range(len(data))]
    t = Table(data, colWidths=widths, rowHeights=heights, hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _total(view: dict, st: dict, width: float) -> Table | None:
    total = view.get("recommended_total")
    if not total:
        return None
    est = total.get("estimated")  # a 6-month price was doubled
    # estimated too when a picked price is one the carrier calls an estimate
    carrier_est = any(_is_estimate(t, i) for t in view["tabs"] if t.get("recommended")
                      for i, c in enumerate(t["columns"]) if c["quote_id"] == t["recommended"])
    right = (f'<font name="Manrope-ExtraBold" size="13" color="{_hex(BLUE)}">{escape(total["amount"])}</font> a year'
             f'{" (estimated)" if est or carrier_est else ""}')
    if total.get("per_month"):
        right += f" · about {escape(total['per_month'])} a month"
    rows = [[_p("Everything above, together", st["total_l"]), Paragraph(right, st["total_r"])]]
    if total.get("left_out"):
        rows.append([_p(f"Does not include {', '.join(x.lower() for x in total['left_out'])} (priced monthly).", st["small"]), ""])
    if est:  # why it is estimated
        rows.append([_p("Prices quoted for 6 months are counted twice for the year.", st["small"]), ""])
    t = Table(rows, colWidths=[width * 0.4, width * 0.6], hAlign="LEFT")
    t.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.75, SILVER), ("LINEBELOW", (0, -1), (-1, -1), 0.75, SILVER),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def _cell(cell: dict, style) -> Paragraph | list:
    """A table cell. Several lines (one per vehicle, in the Vehicles row's order) get a small gap
    between them, so a value that wraps in a narrow column still reads as one vehicle's; a note
    ("same for every vehicle") stays right under the last line."""
    if cell["state"] == "not_listed" or len(cell["lines"]) < 2:
        return _p(_cell_text(cell), style)
    spaced = ParagraphStyle(style.name + "_spaced", parent=style, spaceBefore=2.6)
    out = [_p(x, style if i == 0 else spaced) for i, x in enumerate(cell["lines"])]
    if cell.get("note"):
        out.append(_p(f"({cell['note']})", style))
    return out


def _cell_text(cell: dict) -> str:
    if cell["state"] == "not_listed":
        return "Not listed"
    text = "\n".join(cell["lines"])
    if cell.get("note"):
        text += f"\n({cell['note']})"
    return text


def _sized(st: dict, n: int) -> dict:
    """The table's text styles for n quotes side by side: five get a little smaller type."""
    if n < 5:
        return st
    out = dict(st)
    for key, size, leading in (("th", 7.6, 9.8), ("td", 7.5, 9.7), ("label", 7.5, 9.7), ("gloss", 6.4, 8.1)):
        out[key] = ParagraphStyle(key + "5", parent=st[key], fontSize=size, leading=leading)
    return out


def _table(tab: dict, cols: list[int], st: dict, width: float, opts: dict) -> Table:
    rec = tab.get("recommended")
    columns = [tab["columns"][i] for i in cols]
    st = _sized(st, len(columns))
    tag = (f'<br/><font name="Manrope-Bold" size="5.8" color="{_hex(BLUE)}" backColor="{_hex(SAND)}">'
           '&nbsp;RECOMMENDED&nbsp;</font>')
    data = [[""] + [Paragraph(escape(c["carrier"] or "Unknown carrier") + (tag if c["quote_id"] == rec else ""), st["th"])
                    for c in columns]]
    td = [st["td"]] * len(columns)
    units = {p.get("unit") for p in tab.get("prices") or [] if p.get("unit")}
    price_label = "Premium" if tab["line"] == "life" else "Price" if "month" in units else "Price per year"
    data.append([_p(price_label, st["label"])] + [_price_cell(tab, i, td[k]) for k, i in enumerate(cols)])
    for r in tab["rows"]:
        if r["section"] not in ("vehicles", "core", "also"):
            continue
        cells = [r["cells"][i] for i in cols]
        if all(c["state"] == "not_listed" for c in cells):
            continue  # a row none of these quotes lists
        label = [_p(r["label"], st["label"])]
        if opts.get("gloss") and r.get("help"):
            label.append(_p(r["help"], st["gloss"]))
        data.append([label] + [_cell(c, td[k] if c["state"] != "not_listed" else st["label"])
                               for k, c in enumerate(cells)])
    # the coverage names take less room as quotes are added, so five still fit side by side
    label_w = width * {1: 0.32, 2: 0.32, 3: 0.3, 4: 0.27}.get(len(columns), 0.22)
    col_w = (width - label_w) / max(1, len(columns))
    # splitInRow: a cell taller than a page (dozens of scheduled items) continues on the next page
    t = Table(data, colWidths=[label_w] + [col_w] * len(columns), repeatRows=1, hAlign="LEFT", splitInRow=1)
    pad = 4 if len(columns) >= 5 else 5
    ts = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
          ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
          ("TOPPADDING", (0, 0), (-1, 0), 5),
          ("LINEBELOW", (0, 0), (-1, 0), 0.75, SILVER),
          ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE])]
    for j, c in enumerate(columns, start=1):
        if c["quote_id"] == rec:  # after the stripes, so the whole column is one tint
            ts.append(("BACKGROUND", (j, 0), (j, -1), REC_BG))
    t.setStyle(TableStyle(ts))
    return t


def _chunks(n: int) -> list[list[int]]:
    """Quote columns per table: up to MAX_COLS in one; more split evenly (6 = 3 + 3, 9 = 5 + 4)."""
    idx = list(range(n))
    if n <= MAX_COLS:
        return [idx]
    parts = -(-n // MAX_COLS)
    size = -(-n // parts)
    return [idx[i:i + size] for i in range(0, n, size)]


def _good_to_know(tab: dict, cols: list[int], st: dict) -> Paragraph | None:
    parts = []
    for i in cols:  # one mention of each carrier, its sentences after it
        carrier = tab["columns"][i]["carrier"] or "Unknown carrier"
        texts = [escape(n["text"]) for n in _notices(tab, i) if n.get("key") in CLIENT_NOTICES and n.get("text")]
        if texts:
            parts.append(f"{escape(carrier)}: {' '.join(texts)}")
    if not parts:
        return None
    return Paragraph(f'<font name="Plex-SemiBold" color="{_hex(BLUE)}">Good to know.</font> ' + " ".join(parts), st["gtk"])


def _note(notes: str, st: dict, width: float) -> Table:
    # one table row per paragraph (not one cell), so notes of any length flow across pages
    paras = [x.strip() for x in re.split(r"\n\s*\n|\n", notes) if x.strip()]
    t = Table([[_p(x, st["note"])] for x in paras], colWidths=[width], hAlign="LEFT", splitInRow=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
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
                          topMargin=FIRST_HEAD + 16, bottomMargin=bottom,
                          title=f"{TITLE} for {comp['client']['name']}",
                          author=agency["name"], subject=TITLE, creator=agency["name"])
    first = Frame(MARGIN, bottom, width, h - FIRST_HEAD - 16 - bottom, id="first",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    later = Frame(MARGIN, bottom, width, h - LATER_HEAD - 16 - bottom, id="later",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="first", frames=[first], onPage=_first_page(comp, agency), autoNextPageTemplate="later"),
                          PageTemplate(id="later", frames=[later], onPage=_later_page(comp, agency))])

    number = 0

    def heading(text: str, same: bool = False) -> _Heading:
        nonlocal number
        number += 0 if same else 1  # a policy split over two tables keeps one number
        return _Heading(number, text, width)

    story = [_p(TITLE, st["title"])]
    prop = (comp["client"].get("property_address") or "").strip()
    if prop:  # the home or rental these quotes are for, when it is not the client's own address
        story += [Spacer(1, 3), Paragraph(f'<font name="Plex-SemiBold" size="6.6" color="{_hex(GREY)}">PROPERTY</font>'
                                          f'&nbsp;&nbsp;&nbsp;{escape(prop)}', st["property"])]
    story.append(Spacer(1, 10))
    head = len(story)
    cards = _cards(view, st, width)
    if cards:
        block = [heading("Our recommendation"), Spacer(1, 9), cards]
        total = _total(view, st, width) if opts["total"] else None
        if total:
            block += [Spacer(1, 9), total]
        story += [KeepTogether(block), Spacer(1, 16)]
    elif opts["length"] == "short" and view["tabs"]:
        story += [_p("No recommendation has been picked yet.", st["body"]), Spacer(1, 12)]
    for tab in view["tabs"] if opts["length"] == "full" else []:
        chunks = _chunks(len(tab["columns"]))
        for n, cols in enumerate(chunks):
            title = f"{tab['label']} insurance quotes" if tab["line"] != "unknown" else "Other quotes"
            if len(chunks) > 1:
                title += f" ({n + 1} of {len(chunks)})"
            # a section starts on a new page only when less than a heading and a few rows would fit;
            # long tables then continue on the next page with their column headings repeated
            story += [CondPageBreak(130), heading(title, same=n > 0), Spacer(1, 2), _table(tab, cols, st, width, opts)]
            gtk = _good_to_know(tab, cols, st) if opts["gtk"] else None
            if gtk:
                story += [Spacer(1, 6), gtk]
            story.append(Spacer(1, 16))
    notes = (comp.get("notes") or "").strip()
    if notes:
        story += [CondPageBreak(80), heading("Note from us"), Spacer(1, 8), _note(notes, st, width)]
    if len(story) == head and not view["tabs"]:
        story.append(_p("No quotes have been added yet.", st["body"]))
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(*a, agency=agency, **k))
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
