"""Step 4: Verify. Plain code checks on the extraction; sets a check state on every value.

States: `checked` (grounded and confirmed by a passing sum), `found` (grounded: on the page and
copied exactly), `review` (failed a check; the reason says why). Grounding runs on the redacted
text, because that is what the LLM saw and quoted.

    python -m app.verify fixtures/X.pdf      # uses the cached extraction; no API call
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal

from app import money
from app.config import lines_config, settings

DASHES = "‐‑‒–—―−"
QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"'}

NOT_ON_PAGE = "not found on the page"
VALUE_NOT_IN_SOURCE = "value is not in the line it was read from"
PREMIUM_NOT_IN_SOURCE = "premium is not in the line it was read from"


def norm(s: str | None) -> str:
    s = s or ""
    # the model sometimes writes a line break as the two characters \n; page text never has them
    s = s.replace("\\n", " ").replace("\\t", " ")
    for d in DASHES:
        s = s.replace(d, "-")
    for k, v in QUOTES.items():
        s = s.replace(k, v)
    s = s.replace("*", "")
    return re.sub(r"\s+", " ", s).strip()


def _nospace(s: str) -> str:
    return re.sub(r"\s+", "", s)


class PageText:
    """Normalized page text of one file, for substring checks."""

    def __init__(self, pages: list[dict]):
        self.all = norm(" ".join(p["text"] for p in pages))
        self.ocr_nospace = [_nospace(norm(p["text"])) for p in pages if p["source"] == "ocr"]
        self.lines = [norm(ln) for p in pages for ln in p["text"].splitlines() if ln.strip()]
        self.lines_nospace = [_nospace(ln) for ln in self.lines]

    def with_next_line(self, source: str | None) -> str | None:
        """The page line that contains `source`, plus the following line: for a value that
        continues onto the next line when the model quoted only the first one. Only a continuation
        ("$250 deductible") counts, never a next line that names another coverage (its own row)."""
        n = norm(source)
        if not n:
            return None
        for i, line in enumerate(self.lines):
            if n in line and i + 1 < len(self.lines):
                return None if _words(self.lines[i + 1]) else line + " " + self.lines[i + 1]
        return None

    def contains_row(self, source: str | None, values: list[str]) -> bool:
        """A row whose label wraps onto the next lines, which the model joined into one line.

        True if every word of `source` is within 3 consecutive page lines, and every value sits on
        the line where the label starts. A label from one row stitched to another row's numbers
        fails, because the numbers are not on the label's line."""
        words = norm(source).split()
        if len(words) < 2:
            return False
        head = " ".join(words[:2])
        for i, line in enumerate(self.lines):
            if head not in line:
                continue
            window = " ".join(self.lines[i:i + 3]).split()
            pool = list(window)
            if all(w in pool and not pool.remove(w) for w in words) and \
                    all(_bounded(norm(v), line) for v in values if v):
                return True
        return False

    def contains(self, s: str | None) -> bool:
        n = norm(s)
        if not n:
            return False
        if n in self.all:
            return True
        # OCR spacing differs from what was copied: compare with all spaces removed
        ns = _nospace(n)
        if any(ns in page for page in self.ocr_nospace):
            return True
        return self.contains_lines(s)

    def contains_lines(self, s: str | None, max_gap: int = 3) -> bool:
        """A source quoting several page lines (a table header over its values): each quoted line is
        on the page, in order, at most `max_gap` lines after the previous one (a skipped line between
        them is fine). Spaces are ignored, as OCR spacing varies."""
        parts = [_nospace(norm(p)) for p in re.split(r"\n|\\n", s or "") if p.strip()]
        if len(parts) < 2:
            return False
        starts = [i for i, ln in enumerate(self.lines_nospace) if parts[0] in ln]
        for start in starts:
            pos, ok = start, True
            for part in parts[1:]:
                nxt = next((i for i in range(pos + 1, min(pos + 1 + max_gap, len(self.lines_nospace)))
                            if part in self.lines_nospace[i]), None)
                if nxt is None:
                    ok = False
                    break
                pos = nxt
            if ok:
                return True
        return False


def _bounded(v: str, src: str) -> bool:
    """v occurs in src and is not part of a bigger number ("50,000" is not in "250,000", and
    "$1,447,000" is not in "$1,447,000.00")."""
    if not v:
        return False
    left = r"(?<![\d.,])" if v[0].isdigit() else ""
    right = r"(?![\d]|[.,]\d)" if v[-1].isdigit() else ""
    return re.search(left + re.escape(v) + right, src) is not None


def in_source(value: str, source: str) -> bool:
    v, src = norm(value), norm(source)
    return _bounded(v, src) or _bounded(_nospace(v), _nospace(src))


_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_WELL_FORMED = re.compile(r"^(\d{1,3}(,\d{3})+|\d+)(\.\d{2})?$")
MALFORMED = "a number looks misread (check commas and decimal point)"


def malformed_numbers(s: str | None) -> list[str]:
    """Numbers that are not well-formed amounts, e.g. '1,447,000,00' (an OCR comma for a decimal
    point) or '1,4470'. Grounding cannot catch these: they are on the page, just misread."""
    return [t for t in _NUMBER.findall(s or "") if not _WELL_FORMED.match(t)]


def _premium_parts(premium: str | None) -> list[str]:
    return [p.strip() for p in (premium or "").split("|") if p.strip()]


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def check_item(item: dict, page: PageText, core_keys: set[str]) -> dict:
    reasons = []
    values = [v for v in [item.get("value")] + _premium_parts(item.get("premium")) if not _blank(v)]
    if not page.contains(item.get("source")) and not page.contains_row(item.get("source"), values):
        reasons.append(NOT_ON_PAGE)
    if not _blank(item.get("value")) and not in_source(item["value"], item.get("source", "")):
        extended = page.with_next_line(item.get("source"))
        if not (extended and in_source(item["value"], extended)):
            reasons.append(VALUE_NOT_IN_SOURCE)
    if any(not in_source(p, item.get("source", "")) for p in _premium_parts(item.get("premium"))):
        reasons.append(PREMIUM_NOT_IN_SOURCE)
    if malformed_numbers(item.get("value")) or malformed_numbers(item.get("premium")):
        reasons.append(MALFORMED)
    if item.get("key") in core_keys and _blank(item.get("value")):
        reasons.append("no value printed for this coverage")
    return {"state": "review" if reasons else "found", "reasons": reasons}


TWO_VALUES = "two values found for one coverage"
UNKNOWN_VEHICLE = "vehicle is not in this quote's vehicle list"
WRONG_SECTION = "printed under a deductibles heading, but this row is for a limit"


def _review(item: dict, reason: str) -> None:
    if reason not in item["check"]["reasons"]:
        item["check"]["reasons"].append(reason)
    item["check"]["state"] = "review"


# ---------------------------------------------------------------- right line, wrong row

# Words that say nothing about which coverage a line is about.
_GENERIC = set("""coverage coverages covg cov limit limits deductible deductibles value values amount amounts per each
the and for with not this that when also called usually printed shown same line lines any all its from into
item items key keys record copy exactly including include included includes where offered one category
policy premium premiums optional endorsement form like such only other than vs if e g as by on of or to in
is are be it an person persons accident accidents occurrence day days max maximum month months year years
actual cash disablement""".split())


# Common insurance phrasings folded into one word first, so e.g. "Other Than Collision" is not
# read as "Collision", and "UM Property Damage" is not read as "Property Damage".
_PHRASES = [
    (r"\bcov(?:erage|g)?\.?\s*([a-f])\b(?![\w'])", r" coverage\1 "),
    (r"\bother[\s-]+than[\s-]+collision\b|\botc\b", " comprehensive "),
    (r"\b(?:uninsured|um)[\s/]*(?:underinsured)?\s*(?:motorists?)?\s*(?:property\s+damage|pd)\b|\bumpd\b", " umpd "),
    (r"\b(?:uninsured|um)[\s/]*(?:underinsured|uim)?\s*(?:motorists?)?\s*(?:bodily\s+injury|bi)\b|\bumbi\b", " umbi "),
]


def _words(text: str) -> set[str]:
    t = (text or "").lower()
    for pat, rep in _PHRASES:
        t = re.sub(pat, rep, t)
    return {w for w in re.findall(r"[a-z]{3,}", t) if w not in _GENERIC}


def key_words(line_cfg: dict) -> dict[str, tuple[set[str], set[str]]]:
    """Each coverage key's (name words, description words), from lines.yaml."""
    keys = {**(line_cfg.get("core") or {}), **(line_cfg.get("extras") or {})}
    return {k: (_words(v.get("label", "")), _words(v.get("desc") or "")) for k, v in keys.items()}


def _score(have: set[str], kw: tuple[set[str], set[str]]) -> int:
    name, desc = kw
    return 2 * len(have & name) + len(have & (desc - name))


def row_named_in_line(key: str, source: str | None, words: dict) -> str | None:
    """The key whose words the copied line matches clearly better than the item's own key, or None.

    'Coverage B - Other Structures $144,700' filed as the dwelling row -> 'cov_b_other_structures'.
    The row's own name counts double. A line with no coverage words (a bare row of numbers), a
    tie, or a close call passes: a false alarm on a correct value costs the broker more."""
    if key not in words:
        return None
    have = _words(source)
    own = _score(have, words[key])
    scores = sorted(((_score(have, w), k) for k, w in words.items() if k != key), reverse=True)
    if not scores:
        return None
    best_n, best = scores[0]
    named = {k for k, (name, _) in words.items() if name and len(have & name) >= min(2, len(name))}
    if len(named) >= 4:
        return None  # a header row naming many coverages at once proves nothing
    if best_n > own and (own == 0 or best_n - own >= 2):
        # a deductible and the coverage it belongs to are one coverage in two roles, often on one
        # line ("Hull / Physical Damage  $38,500  1% ($385)  $412.00"): not a different row
        if "deductible" in key and "deductible" not in best and words[best][0] & (words[key][0] | words[key][1]):
            return None
        return best
    return None


ROW_NAMED = "the line it came from names another coverage: "
_AMOUNT = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?%?\)?")


def _row_named_before(it: dict, words: dict) -> str | None:
    """The row named by the words printed right before the value, on the value's own line.

    A source may quote two rows ("Personal Liability $100,000 Medical Payments $1,000"): the whole
    line is a tie, but the words just before "$1,000" name Medical Payments, not Liability. Words
    naming several rows are a header, not this value's label, and prove nothing."""
    v = norm(it.get("value"))
    if not v or not re.search(r"\d", v):
        return None
    line = next((norm(ln) for ln in re.split(r"\n|\\n", it.get("source") or "") if v in norm(ln)), "")
    at = line.find(v)
    if at < 0:
        return None
    have = _words(_AMOUNT.split(line[:at])[-1])
    if not have or len({k for k, (name, _) in words.items() if name and have & name}) > 1:
        return None
    return row_named_in_line(it.get("key"), " ".join(have), words)


def check_structure(q: dict, line_cfg: dict) -> None:
    """Checks that grounding cannot make: a correct number with the wrong role or in the wrong place.

    - A key that should appear once has two items (without a vehicle, or twice for one vehicle).
    - An item names a vehicle that is not in the quote's vehicle list.
    - The copied line names a different coverage than the row it was filed under.
    - A limit key printed under a deductibles heading (only when the extraction records the
      `section`). The reverse is not checked: some carriers print deductibles inside the limits table.
    """
    keys_cfg = {**(line_cfg.get("core") or {}), **(line_cfg.get("extras") or {})}
    words = key_words(line_cfg)
    for it in q.get("items", []):
        other = row_named_in_line(it.get("key"), it.get("source"), words) or _row_named_before(it, words)
        if other:
            _review(it, ROW_NAMED + keys_cfg[other]["label"])
    vehicles = {norm(v).lower() for v in q.get("vehicles") or []}
    seen: dict[tuple, list[dict]] = {}
    for it in q.get("items", []):
        key = it.get("key")
        vehicle = norm(it.get("vehicle")).lower() or None
        if vehicle and vehicles and vehicle not in vehicles:
            _review(it, UNKNOWN_VEHICLE)
        if key != "other" and not (keys_cfg.get(key) or {}).get("multiple"):
            seen.setdefault((key, vehicle), []).append(it)
        section = norm(it.get("section")).lower()
        if section and key != "other":
            if "deductible" not in key and "deductible" in section and "limit" not in section:
                _review(it, WRONG_SECTION)
    for group in seen.values():
        if len(group) > 1:
            for it in group:
                _review(it, TWO_VALUES)


def check_notice(notice: dict, page: PageText) -> dict:
    reasons = []
    if not page.contains(notice.get("source")):
        reasons.append(NOT_ON_PAGE)
    if not in_source(notice.get("text", ""), notice.get("source", "")):
        reasons.append("text is not in the line it was read from")
    return {"state": "review" if reasons else "found", "reasons": reasons}


_TERM_WORDS = {6: r"(?:6|six)", 12: r"(?:12|twelve)"}
_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})(?!\d)")


TERM_NOT_FOUND = "policy term not found on the page"
OTHER_PIF_TOTAL = "the quote prints a different pay-in-full total"
_ANNUAL = re.compile(r"(?i)\b(?:annual|yearly|one[\s-]year|1[\s-]year|12[\s-]month)")


def term_stated(months: int, text: str | None) -> bool:
    """`text` itself states the term: an N-month phrase, two dates N months apart, or 'annual' (12)."""
    return term_supported(months, PageText([{"text": text or "", "source": "text"}])) or \
        (months == 12 and bool(_ANNUAL.search(text or "")))


def term_supported(months: int, page: PageText) -> bool:
    """The page itself supports the term: an "N month" phrase, or two dates exactly N months apart.

    Used when the model's `term_source` is not found word for word (it sometimes joins a header
    row and a values row into one sentence). Checks the fact, not the wording."""
    if re.search(rf"(?i)(?<![\d.]){_TERM_WORDS[months]}[\s-]*month(?!ly)", page.all):  # OCR may merge "a6 month"
        return True
    dates = set()
    for m, d, y in _DATE.findall(page.all):
        yy = int(y) + (2000 if len(y) == 2 else 0)
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            dates.add((yy, int(m), int(d)))
    for y, m, d in dates:
        total = y * 12 + (m - 1) + months
        if (total // 12, total % 12 + 1, d) in dates:
            return True
    return False


def check_sum(pp: dict | None, page: PageText, tolerance: Decimal) -> dict:
    """The premium_parts hint: pass / mismatch / none (no breakdown) / skipped (not checkable)."""
    if not pp or not pp.get("parts"):
        return {"result": "none", "detail": "no breakdown recorded"}
    amounts = [p for p in pp["parts"] if not money.is_no_charge(p)]
    ungrounded = [p for p in amounts + [pp.get("stated_total")] if not page.contains(p)]
    if ungrounded:
        return {"result": "skipped", "detail": f"not found on the page: {', '.join(map(str, ungrounded))}"}
    values = [money.parse(p) for p in amounts]
    total = money.parse(pp.get("stated_total"))
    if total is None or any(v is None for v in values):
        return {"result": "skipped", "detail": "a part or the total is not a single amount"}
    added = sum(values, Decimal(0))
    if abs(added - total) <= tolerance:
        return {"result": "pass", "detail": f"parts add up to {money.fmt(added)}, document says "
                                            f"{money.fmt(total)}", "added": str(added), "total": str(total)}
    return {"result": "mismatch", "detail": f"Line items add up to {money.fmt(added)}, document says "
                                            f"{money.fmt(total)}", "added": str(added), "total": str(total)}


def _distinct_charges(charges: list[dict]) -> list[dict]:
    """A fee is often mentioned twice ("including a $15 policy fee" and "Policy Fee 15.00"). Charges
    with the same kind, amount, and label (ignoring case, or one label inside the other) count once."""
    out: list[dict] = []
    for c in charges:
        amount, label = money.parse(c.get("value")), norm(c.get("label")).lower()
        dup = any(o.get("kind") == c.get("kind") and money.parse(o.get("value")) == amount and
                  (label in norm(o.get("label")).lower() or norm(o.get("label")).lower() in label) for o in out)
        if not dup:
            out.append(c)
    return out


def check_anatomy(pf: dict | None, page: PageText, tolerance: Decimal) -> dict:
    """Code-side check of `price_facts`: premium + fees + taxes against the printed total of premium
    and charges. Discounts, installment fees, and card surcharges are not part of that total."""
    pf = pf or {}
    prem, total = pf.get("premium"), pf.get("premium_and_charges_total")
    charges = _distinct_charges([c for c in pf.get("charges") or [] if c.get("kind") in ("fee", "tax")])
    if not prem or not total:
        return {"result": "none", "detail": "premium or total not printed"}
    if not charges and money.parse(prem.get("value")) == money.parse(total.get("value")):
        return {"result": "none", "detail": "premium and total are the same printed amount"}
    parts = [prem] + charges
    ungrounded = [x.get("value") for x in parts + [total] if not page.contains(x.get("value"))]
    if ungrounded:
        return {"result": "skipped", "detail": f"not found on the page: {', '.join(map(str, ungrounded))}"}
    values = [money.parse(x.get("value")) for x in parts]
    t = money.parse(total.get("value"))
    if t is None or any(v is None for v in values):
        return {"result": "skipped", "detail": "an amount is not a single amount"}
    added = sum(values, Decimal(0))
    what = f"premium, fees and taxes add up to {money.fmt(added)}, document says {money.fmt(t)}"
    return {"result": "pass" if abs(added - t) <= tolerance else "mismatch", "detail": what,
            "added": str(added), "total": str(t)}


def combine_sums(parts: dict, anatomy: dict) -> dict:
    """One sum result from the two checks: any mismatch wins, then any pass, then skipped, then none."""
    for result in ("mismatch", "pass", "skipped"):
        for c in (parts, anatomy):
            if c["result"] == result:
                return c
    return parts


NO_POLICY_TERM = {"life"}  # lines priced without a 6- or 12-month policy term


def verify_quote(q: dict, page: PageText, cfg: dict | None = None) -> dict:
    """Returns the quote with `check` on every item, notice, and the price, plus `checks` and
    `app_notices` (notices written by code, not the LLM)."""
    cfg = cfg or lines_config()
    tolerance = Decimal(str(settings()["verify"]["sum_tolerance"]))
    core_keys = set(((cfg["lines"].get(q.get("line")) or {}).get("core")) or {})
    q = json.loads(json.dumps(q))
    for item in q.get("items", []):
        item["check"] = check_item(item, page, core_keys)
    check_structure(q, cfg["lines"].get(q.get("line")) or {})
    for notice in q.get("notices", []):
        notice["check"] = check_notice(notice, page)

    app_notices = []
    price = q.get("price") or {}
    reasons = []
    if _blank(price.get("value")):
        reasons.append("no price found")
    else:
        if not page.contains(price.get("source")):
            reasons.append(NOT_ON_PAGE)
        if not in_source(price["value"], price.get("source", "")):
            reasons.append(VALUE_NOT_IN_SOURCE)
        if malformed_numbers(price["value"]):
            reasons.append(MALFORMED)
    if q.get("line") in NO_POLICY_TERM:
        pass  # life: the coverage term is in years (the term_length item), not a 6/12-month policy term
    elif q.get("term_months") not in (6, 12):
        reasons.append("policy term is not stated as 6 or 12 months")
    else:
        # the copied line must itself state the term; the page alone counts only when it supports
        # this term and not the other ("Pay in 6 monthly installments" is no 6-month policy)
        tm, ts = q["term_months"], q.get("term_source")
        other = 12 if tm == 6 else 6
        stated = bool(ts) and page.contains(ts) and term_stated(tm, ts)
        implied = term_supported(tm, page) and not term_supported(other, page) and not term_stated(other, ts)
        if not (stated or implied):
            reasons.append(TERM_NOT_FOUND)
    if price.get("basis") == "pay_in_full":
        pf = (q.get("price_facts") or {}).get("pay_in_full_total")
        a, b = money.parse(pf.get("value") if isinstance(pf, dict) else pf), money.parse(price.get("value"))
        if a is not None and b is not None and a != b:
            reasons.append(OTHER_PIF_TOTAL)
    if price.get("basis") == "monthly":
        app_notices.append({"key": "monthly_only", "text": "Only a monthly premium is shown"})
    elif price.get("basis") != "pay_in_full":
        app_notices.append({"key": "no_pay_in_full",
                            "text": "No pay-in-full price on this quote; showing the stated total"})

    s_parts = check_sum(q.get("premium_parts"), page, tolerance)
    s_anat = check_anatomy(q.get("price_facts"), page, tolerance)
    s = combine_sums(s_parts, s_anat)
    state = "review" if reasons else "found"
    if s["result"] == "mismatch":
        note = "Line items add up to {}, document says {}".format(money.fmt(Decimal(s["added"])),
                                                                   money.fmt(Decimal(s["total"])))
        reasons.append(note)
        app_notices.append({"key": "sum_mismatch", "text": note})
        state = "review"
    elif s["result"] == "pass" and state == "found":
        price_value = money.parse(price.get("value"))
        # the sum confirms the price only when its total is the price itself (Progressive-style
        # installment totals differ from the pay-in-full price: the price stays `found`)
        if price_value is not None and price_value == Decimal(s["total"]):
            state = "checked"
    price["check"] = {"state": state, "reasons": reasons}
    q["price"] = price
    q["checks"] = {"sum": s, "sum_parts": s_parts, "sum_anatomy": s_anat}
    q["app_notices"] = app_notices
    return q


def verify(quotes: list[dict], pages: list[dict]) -> list[dict]:
    page = PageText(pages)
    cfg = lines_config()
    return [verify_quote(q, page, cfg) for q in quotes]


def main(argv: list[str] | None = None) -> None:
    from app import extract
    from app.read import read_file
    from app.redact import Client, redact_pages
    ap = argparse.ArgumentParser(description="Check a file's cached extraction.")
    ap.add_argument("file")
    ap.add_argument("--name", default="")
    args = ap.parse_args(argv)
    pages, _ = redact_pages(read_file(args.file), Client(args.name))
    for q in verify(extract.extract(pages, cache_only=True)["quotes"], pages):
        print(f"== {q['line']} {q['carrier']}: price {q['price']['value']} -> {q['price']['check']}")
        print(f"   sum: {q['checks']['sum']}")
        for it in q["items"]:
            print(f"   {it['check']['state']:7} {it['key']:28} {it['value']!s:30} {'; '.join(it['check']['reasons'])}")


if __name__ == "__main__":
    main(sys.argv[1:])
