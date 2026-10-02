"""What the broker's screen gets: the saved comparison with the broker's edits applied, the built
tables, and every check reason turned into a plain sentence. Computed on every request, never stored.

The internal words (found / checked / grounding / OCR / LLM) stop here: the UI gets `state` as
`review`, `ok`, or `not_listed`, and `why` as sentences written for a broker.

Display math (price differences, "best" marks, bars, differences from the current policy, the
drafted reasons) follows the same rule as "Lowest price": it uses only values that are trusted (not
waiting for a check) and hold exactly one dollar amount. Anything else gets no number at all.
"""
from __future__ import annotations

import copy
import re
import uuid

from decimal import Decimal

from app import build, money, storage, verify
from app.config import lines_config, settings

# A value that says the coverage is not part of this quote. ("None" and "$0" are left out on purpose:
# in a deductible row they mean no deductible, the best case.) The screen uses the cell's `excluded`
# flags, so this is the only copy of the list.
EXCLUDED = re.compile(r"^(excluded|rejected|no coverage|not covered|not included|declined|not selected"
                      r"|not purchased|not elected|not available)$", re.I)
_DOLLARS = re.compile(r"\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")

# check reason (app/verify.py) -> what the broker reads in the cell dialog
REASONS = {
    verify.NOT_ON_PAGE: "We couldn't find this line on the quote. Compare it with the page below.",
    verify.VALUE_NOT_IN_SOURCE: "This amount doesn't match the line it came from. Compare it with the quote.",
    verify.PREMIUM_NOT_IN_SOURCE: "The premium doesn't match the line it came from. Compare it with the quote.",
    verify.MALFORMED: "This number may have been misread. Check the commas and the decimal point.",
    verify.TWO_VALUES: "The quote shows two different values for this coverage. Keep the right one.",
    verify.UNKNOWN_VEHICLE: "This value names a vehicle that isn't in the quote's vehicle list.",
    verify.WRONG_SECTION: "This was printed under deductibles, but this row is for a limit.",
    "no value printed for this coverage": "The quote lists this coverage without a value.",
    "no price found": "No price was found on this quote. Type it in from the quote.",
    "policy term is not stated as 6 or 12 months": "The policy term isn't clear. Check whether this price is for 6 or 12 months.",
    verify.OTHER_PIF_TOTAL: "The quote prints a different pay-in-full total. Check which price is right.",
    verify.TERM_NOT_FOUND: "We couldn't confirm the policy term on the quote. Check whether this price is for 6 or 12 months.",
    "text is not in the line it was read from": "This note doesn't match the line it came from.",
}
TERM_REASONS = (verify.TERM_NOT_FOUND, "policy term is not stated as 6 or 12 months")
SUM_RE = re.compile(r"^Line items add up to (\S+), document says (\S+)$")
FALLBACK = "Please compare this value with the quote."

BASIS = {"pay_in_full": "Pay-in-full total", "stated_total": "Stated total (no pay-in-full price shown)",
         "monthly": "Monthly premium (no yearly price shown)", "not_found": "No price found"}


def plain(reason: str) -> str:
    if reason.startswith(verify.ROW_NAMED):
        other = reason[len(verify.ROW_NAMED):]
        return f"The line this came from reads like {other}, not this row. Check which row it belongs in."
    m = SUM_RE.match(reason)
    if m:
        return f"The line items add up to {m.group(1)}, but the quote says {m.group(2)}. Check which is right."
    return REASONS.get(reason, FALLBACK)


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


# ---------------------------------------------------------------- edits

def new_edit(target: dict, action: str, value: str | None, old: str | None) -> dict:
    return {"id": uuid.uuid4().hex[:12], "at": storage.now(), "quote_id": target["quote_id"], "key": target["key"],
            "index": target.get("index"), "label": target.get("label"), "vehicle": target.get("vehicle"),
            "action": action, "value": value, "old": old}


def _same_key(a: str, b: str) -> bool:
    return a == b or (a.startswith("other") and b.startswith("other"))


def same_target(edit: dict, target: dict) -> bool:
    return (edit["quote_id"] == target["quote_id"] and _same_key(edit["key"], target["key"])
            and _norm(edit.get("label")) == _norm(target.get("label"))
            and _norm(edit.get("vehicle")) == _norm(target.get("vehicle")))


def _find_item(q: dict, e: dict) -> dict | None:
    """The item an edit points at: by index when the item there still matches, otherwise by
    key + vehicle + label (indexes shift when a file is re-read)."""
    items = q.get("items", [])
    i = e.get("index")
    if isinstance(i, int) and 0 <= i < len(items):
        it = items[i]
        if it["key"] == e["key"] or (e["key"].startswith("other:") and it["key"] == "other"):
            if _norm(it.get("label")) == _norm(e.get("label")) and _norm(it.get("vehicle")) == _norm(e.get("vehicle")):
                return it
    for it in items:
        if _norm(it.get("label")) == _norm(e.get("label")) and _norm(it.get("vehicle")) == _norm(e.get("vehicle")) \
                and (it["key"] == e["key"] or it["key"] == "other"):
            return it
    return None


def apply_edits(quotes: list[dict], edits: list[dict]) -> list[dict]:
    """The broker's edits, in order, on top of the verified quotes. The saved quotes never change:
    undo is removing an edit from the log."""
    quotes = copy.deepcopy(quotes)
    by_id = {q["id"]: q for q in quotes}
    for e in edits:
        q = by_id.get(e["quote_id"])
        if q is None:
            continue
        if e["key"] == "term":  # the broker sets the policy term (the ×2 yearly estimate follows it)
            if e["action"] == "edit" and e.get("value") in ("6", "12"):
                q["term_months"], q["term_edited"] = int(e["value"]), True
                check = (q.get("price") or {}).get("check")
                if check:
                    check["reasons"] = [r for r in check.get("reasons", []) if r not in TERM_REASONS]
                    if check.get("state") == "review" and not check["reasons"]:
                        check["state"] = "found"
            continue
        if e["key"] == "price":
            target = q.setdefault("price", {"value": None, "basis": "not_found", "source": None})
            target.setdefault("check", {"state": "review", "reasons": []})
        elif e.get("index") is None and e["action"] == "edit":
            label = e.get("label") or e["key"]
            key = "other" if e["key"].startswith("other:") else e["key"]
            target = {"key": key, "label": label, "value": None, "premium": None, "vehicle": e.get("vehicle"),
                      "importance": "key", "source": None, "added": True,
                      "check": {"state": "found", "reasons": []}}
            q.setdefault("items", []).append(target)
        else:
            target = _find_item(q, e)
            if target is None:
                continue
        check = target.setdefault("check", {"state": "found", "reasons": []})
        if e["action"] == "accept":
            if e.get("old") is None or _norm(e["old"]) == _norm(target.get("value")):
                check.update(state="found", accepted=True)
        elif e["action"] == "edit":
            if not target.get("edited") and not target.get("added"):
                target["original_value"] = target.get("value")
            target["value"] = e["value"]
            target["edited"] = True
            check.update(state="found", accepted=True)
        elif e["action"] == "clear" and target is not q.get("price"):
            target["removed"] = True
    for q in quotes:
        removed = [it for it in q.get("items", []) if it.get("removed")]
        if removed:
            q["removed_items"] = [{"key": it["key"], "label": it.get("label"), "value": it.get("value"),
                                   "vehicle": it.get("vehicle")} for it in removed]
            q["items"] = [it for it in q["items"] if not it.get("removed")]
    return quotes


# ---------------------------------------------------------------- the view

def _ui_state(state: str) -> str:
    return {"review": "review", "not_listed": "not_listed"}.get(state, "ok")


def _cell(cell: dict) -> dict:
    items = [i for i in cell.get("items", []) if isinstance(i, dict) and "quote_id" in i]
    why = []
    for it in items:
        if it.get("state") == "review":
            for r in it.get("reasons") or ["?"]:
                s = plain(r)
                if s not in why:
                    why.append(s)
    return {
        "lines": cell["lines"], "excluded": [bool(EXCLUDED.match((x or "").strip())) for x in cell["lines"]],
        "note": cell.get("note"), "period": cell.get("period"), "state": _ui_state(cell["state"]), "why": why,
        "edited": any(it.get("edited") for it in items),
        "items": [{"index": it.get("index"), "key": it.get("key"), "label": it.get("label"),
                   "value": it.get("value"), "premium": it.get("premium"), "vehicle": it.get("vehicle"),
                   "source": _display_source(it.get("source")), "page": it.get("page"),
                   "on_page": verify.NOT_ON_PAGE not in (it.get("reasons") or []),
                   "state": _ui_state(it.get("state") or "found"), "edited": it.get("edited", False),
                   "original_value": it.get("original_value"), "added": it.get("added", False),
                   "why": [plain(r) for r in it.get("reasons", [])] if it.get("state") == "review" else []}
                  for it in items],
    }


def _display_source(s: str | None) -> str | None:
    """Redaction placeholders read as plain words in the dialog."""
    if not s:
        return s
    s = (s.replace("[CLIENT_NAME]", "(client name)").replace("[CLIENT_ADDRESS]", "(client address)")
            .replace("[VIN]", "(VIN)").replace("[EMAIL]", "(email)").replace("[PHONE]", "(phone)")
            .replace("[LICENSE]", "(license number)").replace("[DOB]", "(date of birth)"))
    return re.sub(r"\[PERSON_\d+\]", "(household member)", s)


def _pages_text(pages: list[int]) -> str | None:
    if not pages:
        return None
    if len(pages) == 1:
        return f"page {pages[0]}"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"pages {pages[0]} to {pages[-1]}" if len(pages) > 2 else f"pages {pages[0]} and {pages[1]}"
    return "pages " + ", ".join(map(str, pages))


def _notice_text(q: dict, key: str) -> str | None:
    for n in q.get("notices", []):
        if n.get("key") == key and n.get("text"):
            return n["text"]
    return None


def quote_info(q: dict, files: dict, line_labels: dict) -> dict:
    f = files.get(q["file_id"], {})
    file_pages = len(f.get("pages") or [])
    pages = q.get("pages") or []
    return {
        "id": q["id"], "file_id": q["file_id"], "file_name": q.get("file_name"),
        "carrier": build.display_carrier(q.get("carrier")), "line": q["line"],
        "line_label": line_labels.get(q["line"], "Other"), "term_months": q.get("term_months"),
        "term_edited": bool(q.get("term_edited")),
        "price": (q.get("price") or {}).get("value"), "price_basis": BASIS.get((q.get("price") or {}).get("basis")),
        "price_basis_key": (q.get("price") or {}).get("basis"), "term_label": build.term_label(q),
        "discounts": _notice_text(q, "discounts"), "policy_period": _notice_text(q, "policy_period"),
        "other_notices": [n["text"] for n in q.get("notices", []) if n.get("key") == "other_notice" and n.get("text")],
        "pages_text": _pages_text(pages) if file_pages > 1 and len(pages) < file_pages else None,
        "first_page": pages[0] if pages else 1, "added_at": f.get("added_at"),
        "removed_items": q.get("removed_items", []),
        "pages": pages or [1], "file_pages": file_pages or 1, "file_ext": f.get("ext", ".pdf"),
        "expires": _notice_text(q, "quote_expiration"), "redactions": f.get("redactions"),
        "values": len(q.get("items", [])) + (1 if (q.get("price") or {}).get("value") else 0),
    }


def view(comp: dict) -> dict:
    comp = storage.normalize(copy.deepcopy(comp))
    cfg = lines_config()
    line_labels = {k: v["label"] for k, v in cfg["lines"].items()}
    quotes = apply_edits(comp["quotes"], comp.get("edits", []))
    by_id = {q["id"]: q for q in quotes}
    built = build.build({"quotes": quotes, "kept": comp.get("kept", {})})
    files = {f["id"]: f for f in comp["files"]}
    tabs, total_review = [], 0
    for t in built["tabs"]:
        specs = {**(cfg["lines"].get(t["line"], {}).get("core") or {}), **(cfg["lines"].get(t["line"], {}).get("extras") or {})}
        rows = []
        for r in t["rows"]:
            if r["section"] == "good_to_know":
                cells = [{"lines": c["lines"], "state": "ok", "why": [], "edited": False, "note": None,
                          "notices": [{"key": n["key"], "text": n["text"], "document_text": _display_source(n.get("document_text"))}
                                      for n in c["items"]]} for c in r["cells"]]
            else:
                cells = [_cell(c) for c in r["cells"]]
                if r["section"] == "price":
                    for c, raw in zip(cells, r["cells"]):
                        c["sum_check"] = _sum_check(raw)
            spec = specs.get(r["key"], {})
            rows.append({"section": r["section"], "key": r["key"], "label": r["label"], "cells": cells,
                         "kept": r["key"] in (comp.get("kept", {}).get(t["line"]) or []),
                         "group": _group(r), "better": spec.get("better") if spec.get("better") in ("higher", "lower") else None,
                         "help": spec.get("help")})
        review = sum(1 for r in rows if r["section"] != "other" for c in r["cells"] if c["state"] == "review")
        hidden = sum(1 for r in rows if r["section"] == "other" for c in r["cells"] if c["state"] == "review")
        total_review += review
        rec = comp.get("recommendation", {}).get(t["line"])
        rec = rec if rec in {c["quote_id"] for c in t["columns"]} else None
        cur = comp["current"].get(t["line"])
        cur = cur if cur in {c["quote_id"] for c in t["columns"]} else None
        tab = {"line": t["line"], "label": t["label"], "columns": t["columns"], "rows": rows,
               "review": review, "hidden_review": hidden, "recommended": rec, "current": cur}
        tab["prices"] = price_summaries(tab, by_id)
        row_math(tab)
        tab["facts"] = facts(tab, by_id, quotes, comp["fact_choices"].get(t["line"], {})) if rec else []
        tab["own_reasons"] = [dict(r) for r in comp["own_reasons"].get(t["line"], [])] if rec else []
        tab["reasons"] = [f["text"] for f in tab["facts"] if f["on"]] + [r["text"] for r in tab["own_reasons"] if r["on"]]
        tabs.append(tab)
    return {
        "id": comp["id"], "revision": comp["revision"], "client": comp["client"],
        "created_at": comp["created_at"], "updated_at": comp["updated_at"],
        "stage": comp["stage"], "outcome": comp["outcome"], "archived_at": comp.get("archived_at"), "export": comp["export"],
        "notes": comp.get("notes", ""), "tabs": tabs, "review": total_review,
        "quotes": {q["id"]: quote_info(q, files, line_labels) for q in quotes},
        "files": [file_view(f) for f in comp["files"]],
        "lines": [{"key": k, "label": v} for k, v in line_labels.items()] + [{"key": "unknown", "label": "Other"}],
        "reason_suggestions": list((settings().get("recommendation_reasons") or {}).values()),
        "agency": {k: (settings().get("agency") or {}).get(k, "") for k in ("name", "phone")},
        "recommended_total": recommended_total(tabs, by_id),
    }


# ---------------------------------------------------------------- display math (trusted single amounts only)

def amount(text: str | None) -> Decimal | None:
    """The one dollar amount in a value ("$1,447,000", "$500 deductible"), or None when it has
    none, several ("$100,000/$300,000"), or other numbers too ("2% ($7,150)", "$1,000 | 500")."""
    found = _DOLLARS.findall(text or "")
    if len(found) != 1 or re.search(r"\d", _DOLLARS.sub("", text or "")):
        return None
    return money.parse(found[0].replace(" ", ""))


def dollars(value: Decimal) -> str:
    """$1,447,000 for whole dollars, $1,538.40 otherwise."""
    value = abs(value)
    return f"${value:,.0f}" if value == value.to_integral_value() else f"${value:,.2f}"


MISSING, UNTRUSTED = "missing", "untrusted"


def cell_amount(cell: dict):
    """A cell's amount for display math: MISSING (not on the quote), UNTRUSTED (waiting for a check),
    None (not a single dollar amount), or the Decimal."""
    if cell["state"] == "not_listed":
        return MISSING
    if cell["state"] == "review":
        return UNTRUSTED
    lines = cell.get("lines") or []
    return amount(lines[0]) if len(lines) == 1 else None


def _group(r: dict) -> str:
    """Where the row sits in the broker's table: coverage / deductibles (core), also, other."""
    if r["section"] == "core":
        return "deductibles" if "deductible" in r["key"] else "coverage"
    return r["section"]


def _sum_check(raw_cell: dict) -> dict | None:
    """A price whose line items add up to another total: both numbers, so the broker can pick one."""
    for reason in raw_cell.get("reasons") or []:
        m = SUM_RE.match(reason)
        if m:
            return {"parts": m.group(1).rstrip(","), "printed": m.group(2).rstrip(",")}
    return None


def price_summaries(tab: dict, quotes: dict) -> list[dict]:
    """Per column: the price as a yearly (or monthly) amount, and how it compares.

    "Lowest price" and "+$X vs lowest" come only when every price in the tab is trusted and in the
    same unit (the rule build.build_tab uses for its "Lowest price" note). While a price still needs
    a check, the cheapest one says "Lowest if $X is right" instead, and no differences are shown."""
    price_row = next(r for r in tab["rows"] if r["section"] == "price")
    cols = tab["columns"]
    qs = [quotes[c["quote_id"]] for c in cols]
    comparable = [build.comparable_price(q) for q in qs]
    lowest = [c.get("note") == "Lowest price" for c in price_row["cells"]]
    low = lowest.index(True) if any(lowest) else None
    untrusted = [c["state"] == "review" for c in price_row["cells"]]
    units = {c[0] for c in comparable if c}
    candidates = [i for i, c in enumerate(comparable) if c] if len(units) == 1 and len(qs) >= 2 else []
    cheapest = min(candidates, key=lambda i: comparable[i][1]) if candidates else None
    top = max((comparable[i][1] for i in candidates), default=None)
    out = []
    for i, (q, cp) in enumerate(zip(qs, comparable)):
        printed = (q.get("price") or {}).get("value")
        info = {"amount": dollars(cp[1]) if cp else (printed or None), "unit": cp[0] if cp else None,
                "estimated": bool(cp) and q.get("term_months") == 6 and cp[0] == "year",
                "printed": printed, "six_month": q.get("term_months") == 6,
                "lowest": lowest[i], "lowest_if": None, "vs_lowest": None, "bar": None,
                "excludes": excludes(tab, i),
                "below_umbrella": any(n["key"] == "below_umbrella_requirement"
                                      for n in _gtk_cell(tab, i).get("notices", []))}
        if low is not None and cp:
            if i != low:
                diff = cp[1] - comparable[low][1]
                approx = info["estimated"] or (qs[low].get("term_months") == 6)
                info["vs_lowest"] = f"{'≈ ' if approx else ''}+{dollars(diff)} vs lowest" if diff > 0 else None
            if top and top > 0:
                info["bar"] = max(2, round(float(cp[1] / top) * 100))
        elif any(untrusted) and i == cheapest and untrusted[i] and printed:
            info["lowest_if"] = f"Lowest if {printed} is right"
        out.append(info)
    return out


def _gtk_cell(tab: dict, i: int) -> dict:
    row = next((r for r in tab["rows"] if r["section"] == "good_to_know"), None)
    return row["cells"][i] if row else {}


def excludes(tab: dict, i: int) -> list[str]:
    """Coverage rows (in the table, not the hidden ones) that this quote prints as excluded while
    another quote has the coverage. Text only, no math."""
    out = []
    for r in tab["rows"]:
        if r["section"] not in ("core", "also"):
            continue
        mine = r["cells"][i]
        if mine["state"] == "not_listed" or len(mine["lines"]) != 1 or not EXCLUDED.match(mine["lines"][0].strip()):
            continue
        if any(c["state"] != "not_listed" and not all(EXCLUDED.match(x.strip()) for x in c["lines"])
               for j, c in enumerate(r["cells"]) if j != i):
            out.append(short_label(r["label"]))
    return out


def short_label(label: str) -> str:
    """'Coverage A, dwelling' -> 'dwelling'; 'Water backup' -> 'water backup'; 'RV' stays."""
    s = re.sub(r"^Coverage [A-F], ", "", label)
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in s.split())


def row_math(tab: dict) -> None:
    """Adds to each value cell: `best` (the best trusted amount in a row with a better direction),
    `bar` (0-100, rows where higher is better), and `vs_current` (the difference from the client's
    current policy). A row gets no best/bars when any listed value is untrusted or not a single amount."""
    cur = next((i for i, c in enumerate(tab["columns"]) if c["quote_id"] == tab.get("current")), None)
    for r in tab["rows"]:
        if r["section"] not in ("core", "also", "other"):
            continue
        amounts = [cell_amount(c) for c in r["cells"]]
        for c in r["cells"]:
            c.update(best=False, bar=None, vs_current=None)
        listed = [a for a in amounts if a is not MISSING]
        if r["better"] and len(listed) >= 2 and all(isinstance(a, Decimal) for a in listed) and len(set(listed)) >= 2:
            best = max(listed) if r["better"] == "higher" else min(listed)
            top = max(listed)
            for c, a in zip(r["cells"], amounts):
                if a is MISSING:
                    continue
                c["best"] = a == best
                if r["better"] == "higher" and top > 0:
                    c["bar"] = max(2, round(float(a / top) * 100))
        if cur is None:
            continue
        base, base_cell = amounts[cur], r["cells"][cur]
        for j, (c, a) in enumerate(zip(r["cells"], amounts)):
            if j == cur or a in (MISSING, UNTRUSTED) or base in (MISSING, UNTRUSTED):
                continue
            if isinstance(a, Decimal) and isinstance(base, Decimal) and a != base:
                more = a > base
                good = None if not r["better"] else (more if r["better"] == "higher" else not more)
                c["vs_current"] = {"text": f"{dollars(a - base)} {'more' if more else 'less'} than current",
                                   "up": more, "good": good}
            elif (isinstance(a, Decimal) and a == base) or \
                    _norm(" ".join(c["lines"])) == _norm(" ".join(base_cell["lines"])):  # "$1,000.00" is "$1,000"
                c["vs_current"] = {"text": "Same as current", "up": None, "good": None}


# ---------------------------------------------------------------- drafted reasons for a recommended quote

def facts(tab: dict, quotes: dict, all_quotes: list[dict], choices: dict) -> list[dict]:
    """Short facts about the recommended quote, drafted from the table, for the broker to tick.
    Each has a stable id (so a tick survives a re-read) and says which row it came from. Built only
    from trusted values: a row with any value waiting for a check gives no fact."""
    cols = tab["columns"]
    r = next(i for i, c in enumerate(cols) if c["quote_id"] == tab["recommended"])
    others = [i for i in range(len(cols)) if i != r]
    q = quotes[cols[r]["quote_id"]]
    out: list[dict] = []

    def add(fid, text, src, on):
        out.append({"id": fid, "text": text, "src": src, "on": choices.get(fid, on)})

    value_rows = [x for x in tab["rows"] if x["section"] in ("core", "also")]
    if not others:
        for x in [x for x in value_rows if x["section"] == "core"]:
            c = x["cells"][r]
            if c["state"] == "ok" and len(c["lines"]) == 1 and len([f for f in out if f["id"].startswith("value:")]) < 2:
                name = short_label(x["label"])
                add(f"value:{x['key']}", f"{name[:1].upper()}{name[1:]}: {c['lines'][0]}", f"{x['label']} row", True)
    else:
        prices = tab["prices"]
        if prices[r]["lowest"]:
            add("lowest_price", "Lowest yearly price" if prices[r]["unit"] == "year" else "Lowest price", "price row", True)
        for x in value_rows:
            cells = x["cells"]
            if any(c["state"] == "review" for c in cells):
                continue
            mine = cells[r]
            has = lambda c: c["state"] != "not_listed" and not all(EXCLUDED.match(t.strip()) for t in c["lines"])
            if has(mine) and not any(has(cells[j]) for j in others):
                # ticked only when the others print it as excluded; "not listed" means not found on
                # the quote, which is not proof the coverage is missing (the client reads this claim)
                printed = all(cells[j]["state"] != "not_listed" for j in others)
                add(f"only:{x['key']}", f"Only quote with {short_label(x['label'])}", f"{x['label']} row", printed)
        for x in value_rows:
            cells = x["cells"]
            if cells[r].get("best") and sum(1 for c in cells if c.get("best")) == 1:
                word = "Highest" if x["better"] == "higher" else "Lowest"
                add(f"best:{x['key']}", f"{word} {short_label(x['label'])}: {cells[r]['lines'][0]}", f"{x['label']} row",
                    len(out) < 4)
        low = next((i for i, p in enumerate(prices) if p["lowest"]), None)
        if low is not None and low != r and prices[r]["vs_lowest"]:
            about = "about " if prices[r]["vs_lowest"].startswith("≈") else ""
            diff = prices[r]["vs_lowest"].replace(" vs lowest", "").lstrip("≈ +")
            per = "a year" if prices[r]["unit"] == "year" else "a month"
            add("more_than_lowest", f"Costs {about}{diff} {per} more than {cols[low]['carrier']}", "price row", False)
        if prices[r]["excludes"]:
            add("excludes", f"Doesn't include {_join(prices[r]['excludes'])}", "excluded on this quote", False)
    if tab["line"] == "auto":
        carrier = build.meets_umbrella(q, all_quotes)
        if carrier:
            add("umbrella_ok", f"Meets the {carrier} umbrella's required auto limits", "umbrella check", True)
    if any(n.get("key") == "uninsured_motorist_rejected" and n.get("check", {}).get("state") != "review"
           for n in q.get("notices", [])):
        add("um_rejected", "Uninsured motorist coverage is declined on this quote", "Good to know", False)
    return out[:6]


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


# ---------------------------------------------------------------- the board card

_card_cache: dict[tuple, dict] = {}
_DATE_RES = (
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: (int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}),? (\d{4})\b", re.I),
     lambda m: (int(m[3]), ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
                .index(m[1][:3].lower()) + 1, int(m[2]))),
)


def printed_date(text: str | None) -> str | None:
    """The first calendar date printed in a notice ('Quote expires 10/15/2026'), as YYYY-MM-DD.
    Wording without a date ('valid for 30 days') gives None: the app does no date math on it."""
    from datetime import date
    for rx, parts in _DATE_RES:
        m = rx.search(text or "")
        if m:
            try:
                return date(*parts(m)).isoformat()
            except ValueError:
                return None
    return None


def card(comp: dict) -> dict:
    """What a board card shows. Cached per comparison revision (the board polls while files are read)."""
    key = (comp["id"], comp["revision"], comp["updated_at"], comp["stage"], comp.get("outcome"),
           comp.get("archived_at"), str(lines_config().get("config_version")))
    hit = _card_cache.get(key)
    if hit:
        return hit
    v = view(comp)
    active = [f for f in comp["files"] if f["status"] in storage.ACTIVE]
    steps = {"queued": 0, "reading": 0.5, "redacting": 1.5, "extracting": 2.5, "checking": 3.5}
    expires = sorted(d for q in v["quotes"].values() for d in [printed_date(q.get("expires"))] if d)
    picked = [t for t in v["tabs"] if t["recommended"]]
    out = {
        "id": comp["id"], "client_name": comp["client"]["name"], "created_at": comp["created_at"],
        "updated_at": comp["updated_at"], "stage": comp["stage"], "outcome": comp["outcome"],
        "archived_at": comp.get("archived_at"), "quotes": len(v["quotes"]), "files": len(comp["files"]),
        "lines": [{"key": t["line"], "label": t["label"], "count": len(t["columns"])} for t in v["tabs"]],
        "carriers": sorted({c["carrier"] for t in v["tabs"] for c in t["columns"] if c["carrier"]}),
        "review": v["review"], "picked": len(picked),
        "total": v["recommended_total"]["amount"] if v["recommended_total"] else None,
        "total_estimated": bool(v["recommended_total"] and v["recommended_total"]["estimated"]),
        "expires": expires[0] if expires else None,
        "processing": len(active),
        "failed": sum(1 for f in comp["files"] if f["status"] in ("failed", "interrupted", "empty")),
        "progress": round(sum(steps.get(f["status"], 0) for f in active) / (4 * len(active)) * 100) if active else None,
    }
    if len(_card_cache) > 500:
        _card_cache.clear()
    _card_cache[key] = out
    return out


def recommended_total(tabs: list[dict], quotes: dict) -> dict | None:
    """The recommended quotes' yearly prices added up, for the PDF summary. The one sum the app
    prints, so it stays honest: only when 2+ quotes are recommended, every one of those prices is
    trusted (not waiting for a check), and priced per year. A 6-month price counts x2 and makes the
    total an estimate. Monthly prices (life) are never added to yearly ones: they are left out and named."""
    picks = [(t, quotes[t["recommended"]]) for t in tabs if t.get("recommended") in quotes]
    if len(picks) < 2:
        return None
    total, estimated, used, monthly = Decimal(0), False, [], []
    for t, q in picks:
        if (q.get("price") or {}).get("check", {}).get("state") == "review":
            return None
        unit_amount = build.comparable_price(q)
        if unit_amount is None:
            return None
        unit, amount = unit_amount
        if unit == "month":
            monthly.append(t["label"])
            continue
        total += amount
        estimated = estimated or q.get("term_months") == 6
        used.append((t["label"], build.display_carrier(q.get("carrier"))))
    if len(used) < 2:
        return None
    carriers = {c for _, c in used}
    labels = [lab if lab.isupper() else lab.lower() for lab, _ in used]
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    label = f"{next(iter(carriers))} {joined} together" if len(carriers) == 1 else f"Recommended {joined} together"
    amount = money.fmt(total)
    return {"label": label[0].upper() + label[1:], "amount": amount, "estimated": estimated,
            "text": f"{'≈ ' if estimated else ''}{amount} per year{' (estimated)' if estimated else ''}",
            "per_month": f"${(total / 12).quantize(Decimal(1)):,}",  # the yearly total / 12, whole dollars ("about $X a month")
            "left_out": monthly}


def file_view(f: dict) -> dict:
    return {"id": f["id"], "name": f["name"], "status": f["status"], "summary": f.get("summary"),
            "error": f.get("error"), "redactions": f.get("redactions"), "quote_ids": f.get("quote_ids", []),
            "added_at": f.get("added_at"), "job_kind": f.get("job_kind")}
