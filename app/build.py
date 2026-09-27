"""Step 5: Build. Verified quotes -> one comparison table per line of business. Plain code, no LLM.

Input is a comparison: {"quotes": [verified quote + {"id", "file_name"}], "kept": {line: [row keys]}}.
Output (never stored, rebuilt on every request):

    {"tabs": [{"line", "label", "columns": [...], "rows": [...], "needs_check": bool}]}

Each row: {"section", "key", "label", "cells": [cell per column]}; each cell:
{"lines": [str], "state": checked|found|review|not_listed, "reasons": [str], "items": [item refs],
 "note": str|None}. The only math on displayed values is the x2 for 6-month prices.

    python -m tools.run_fixtures --stage tables   # the fixture groups (cached extractions)
"""
from __future__ import annotations

import re
from decimal import Decimal

from app import money
from app.config import lines_config

STATE_RANK = {"review": 3, "found": 2, "checked": 1, "not_listed": 0}
# Lines whose quotes list vehicles (or boats), shown as a row under the price.
VEHICLE_ROW = {"auto": "Vehicles", "motorcycle": "Vehicles", "rv": "Vehicles", "watercraft": "Boat, motor, and trailer"}
SECTIONS = ("price", "vehicles", "core", "also", "other", "good_to_know")
# Order of the "Good to know" sentences.
NOTICE_PRIORITY = ["sum_mismatch", "below_umbrella_requirement", "uninsured_motorist_rejected", "no_pay_in_full", "monthly_only",
                   "estimated_premium", "underlying_auto_bi", "underlying_auto_pd",
                   "underlying_home_liability", "surcharge_or_history", "quote_expiration", "other_notice"]


# ---------------------------------------------------------------- small helpers

def short_vehicle(name: str, all_names: list[str]) -> str:
    """'2008 JEEP PATRIOT 4 DOOR WAGON' -> 'JEEP' (make only, or make + model if two share a make)."""
    words = [w for w in (name or "").split() if not re.fullmatch(r"(19|20)\d\d", w)]
    if not words:
        return name
    makes = [next((w for w in n.split() if not re.fullmatch(r"(19|20)\d\d", w)), "") for n in all_names]
    if makes.count(words[0]) > 1 and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


def category(label: str) -> str:
    """'Coverage C Increased Special Limits Of Liability - Firearms' -> 'Firearms'."""
    parts = re.split(r"\s+-\s*|\s*-\s+", label or "")
    return parts[-1].strip() if len(parts) > 1 and parts[-1].strip() else (label or "")


def worst_state(states: list[str]) -> str:
    return max(states, key=lambda s: STATE_RANK[s]) if states else "not_listed"


def annualized(q: dict) -> Decimal | None:
    """12-month price for comparison: the price itself, or x2 for a 6-month quote (estimated)."""
    value = money.parse((q.get("price") or {}).get("value"))
    if value is None or q.get("term_months") not in (6, 12):
        return None
    return value * 2 if q["term_months"] == 6 else value


def comparable_price(q: dict) -> tuple[str, Decimal] | None:
    """(unit, amount) for "Lowest price": a 6-month price x2 per year, a monthly price per month.
    Prices are only compared within one unit; the app never turns a monthly price into a yearly one."""
    price = q.get("price") or {}
    value = money.parse(price.get("value"))
    if value is None:
        return None
    if price.get("basis") == "monthly":
        return ("month", value)
    if q.get("line") == "life":
        return ("year", value)
    yearly = annualized(q)
    return ("year", yearly) if yearly is not None else None


def price_period(q: dict) -> str | None:
    """Words under the price: 'for 6 months' or 'per month' (null for a 12-month or yearly price)."""
    if (q.get("price") or {}).get("basis") == "monthly":
        return "per month"
    if q.get("term_months") == 6:
        return "for 6 months"
    if q.get("line") == "life" and (q.get("price") or {}).get("value"):
        return "per year"  # next to monthly life prices, a bare yearly price would be ambiguous
    return None


def term_label(q: dict) -> str | None:
    """The column's term chip: '6 months' / '12 months', or a life policy's term length as printed."""
    if q.get("line") == "life":
        return next((it.get("value") for it in q.get("items", []) if it.get("key") == "term_length" and it.get("value")), None)
    return {6: "6 months", 12: "12 months"}.get(q.get("term_months"))


def limit_numbers(text: str | None) -> list[Decimal]:
    """'$100,000 each person/$300,000 each accident' -> [100000, 300000]. Checks only."""
    return [Decimal(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", text or "")]


# ---------------------------------------------------------------- cells

def item_ref(q: dict, it: dict) -> dict:
    return {"quote_id": q["id"], "index": it.get("_idx"), "key": it["key"], "label": it.get("label"), "value": it.get("value"),
            "premium": it.get("premium"), "vehicle": it.get("vehicle"), "source": it.get("source"),
            "state": it["check"]["state"], "reasons": it["check"]["reasons"], "page": it.get("page"),
            "edited": bool(it.get("edited")), "original_value": it.get("original_value"),
            "accepted": bool(it["check"].get("accepted")), "added": bool(it.get("added"))}


def item_text(it: dict) -> tuple[str, str | None]:
    """(what the cell shows, a note under it)."""
    if it.get("value") not in (None, ""):
        return it["value"], None
    if it.get("premium") not in (None, ""):
        return it["premium"], "premium, no limit printed"
    return "listed", None


def _entry(it: dict) -> str:
    """One entry inside a multi-line cell; a premium-only entry says so (a bare "$20.00" would read as a limit)."""
    text, note = item_text(it)
    return f"{text} (premium, no limit printed)" if note else text


def items_cell(q: dict, items: list[dict], multiple: bool = False, per_vehicle: bool = False) -> dict:
    if not items:
        return {"lines": ["—"], "state": "not_listed", "reasons": [], "items": [], "note": None}
    states = [it["check"]["state"] for it in items]
    reasons = sorted({r for it in items for r in it["check"]["reasons"]})
    refs = [item_ref(q, it) for it in items]
    if len(items) == 1 and not multiple and (not items[0].get("vehicle") or not per_vehicle):
        text, note = item_text(items[0])
        return {"lines": [text], "state": states[0], "reasons": reasons, "items": refs, "note": note}
    lines = []
    by_vehicle = [it for it in items if it.get("vehicle")]
    names = q.get("vehicles") or [it["vehicle"] for it in by_vehicle]
    if by_vehicle and not per_vehicle and len(by_vehicle) == len(items) and len(by_vehicle) == len(names) \
            and len({(it.get("value"), bool(it.get("value"))) for it in by_vehicle}) == 1 and by_vehicle[0].get("value"):
        # a policy-level limit printed once per vehicle (e.g. liability): the same for every vehicle
        return {"lines": [by_vehicle[0]["value"]], "state": worst_state(states), "reasons": reasons,
                "items": refs, "note": "same for every vehicle" if len(names) > 1 else None}
    if by_vehicle:
        names = q.get("vehicles") or [it["vehicle"] for it in by_vehicle]
        for v in names:
            match = [it for it in by_vehicle if (it["vehicle"] or "").strip().lower() == v.strip().lower()]
            label = short_vehicle(v, names)
            lines += [(f"{label}: " if len(names) > 1 else "") + _entry(it) for it in match] or [f"{label}: —"]
        # a vehicle name the quote's vehicle list does not have (verify marked it review)
        known = {v.strip().lower() for v in names}
        lines += [f"{it['vehicle']}: {_entry(it)}" for it in by_vehicle if it["vehicle"].strip().lower() not in known]
    rest = [it for it in items if not it.get("vehicle")]
    if multiple:
        lines += [f"{category(it.get('label'))}: {_entry(it)}" for it in rest]
    else:
        lines += [_entry(it) for it in rest]
    return {"lines": lines, "state": worst_state(states), "reasons": reasons, "items": refs, "note": None}


def price_cell(q: dict, lowest: bool) -> dict:
    price = q.get("price") or {}
    lines = [price.get("value") or "—"]
    if q.get("term_months") == 6 and annualized(q) is not None:
        lines.append(f"≈ {money.fmt(annualized(q))} per year (estimated)")
    return {"lines": lines, "state": price.get("check", {}).get("state", "review") if price.get("value") else "review",
            "reasons": price.get("check", {}).get("reasons", []), "note": "Lowest price" if lowest else None,
            "period": price_period(q),
            "items": [{"quote_id": q["id"], "index": None, "key": "price", "value": price.get("value"),
                       "basis": price.get("basis"), "source": price.get("source"), "page": price.get("page"),
                       "state": price.get("check", {}).get("state"), "reasons": price.get("check", {}).get("reasons", []),
                       "edited": bool(price.get("edited")), "original_value": price.get("original_value"),
                       "accepted": bool(price.get("check", {}).get("accepted")), "added": False}]}


def good_to_know(q: dict, cfg: dict) -> list[dict]:
    """Short sentences written by code from the `show` templates in lines.yaml: fixed facts only.

    Free-text notices (`other_notice`), discounts, and the policy period have no template and are
    shown in the quote dialog instead. A totals problem is shown only while the price still needs
    checking; once the broker accepts or edits the price it goes away."""
    shows = {k: v.get("show") for k, v in cfg["notice_keys"].items()}
    price_in_review = (q.get("price") or {}).get("check", {}).get("state") == "review"
    notices = [n for n in q.get("notices", []) if n.get("check", {}).get("state") != "review"]
    notices += q.get("app_notices", []) + q.get("build_notices", [])
    order = {k: i for i, k in enumerate(NOTICE_PRIORITY)}
    out, seen = [], set()
    for n in sorted(notices, key=lambda n: order.get(n["key"], len(order))):
        template = shows.get(n["key"])
        if not template or (n["key"] == "sum_mismatch" and not price_in_review):
            continue
        sentence = template.format(text=n.get("text", ""), carrier=n.get("carrier", ""))
        if sentence not in seen:
            seen.add(sentence)
            out.append({"key": n["key"], "text": sentence, "document_text": n.get("source") or n.get("text")})
    return out


def display_carrier(name: str | None) -> str:
    """'TRAVELERS' -> 'Travelers'. Only names printed in all capitals change."""
    name = name or ""
    return name.title() if name.isupper() and len(name) > 3 else name


# ---------------------------------------------------------------- cross-quote

def umbrella_requirement(quotes: list[dict]) -> None:
    """Adds a `below_umbrella_requirement` build notice to auto quotes whose BI or PD limits are
    below what an umbrella quote in the same comparison requires. Only confirmed values are used."""
    for u in (q for q in quotes if q["line"] == "umbrella"):
        req = {n["key"]: limit_numbers(n.get("text")) for n in u.get("notices", [])
               if n["key"] in ("underlying_auto_bi", "underlying_auto_pd") and n.get("check", {}).get("state") != "review"}
        for a in (q for q in quotes if q["line"] == "auto"):
            below = False
            for key, notice in (("bi_limit", "underlying_auto_bi"), ("pd_limit", "underlying_auto_pd")):
                need = req.get(notice)
                if not need:
                    continue
                for it in a.get("items", []):
                    if it["key"] != key or it["check"]["state"] == "review":
                        continue
                    have = limit_numbers(it.get("value"))
                    if have and any(h < n for h, n in zip(have, need)):
                        below = True
            if below:
                a.setdefault("build_notices", []).append(
                    {"key": "below_umbrella_requirement", "carrier": display_carrier(u.get("carrier")), "text": ""})


def meets_umbrella(auto: dict, quotes: list[dict]) -> str | None:
    """The umbrella carrier whose required auto limits this auto quote is confirmed to meet, or None.
    Confirmed means: the umbrella states a requirement, every required limit has a trusted value on
    the auto quote, and none is below. Anything missing or waiting for a check is not confirmed."""
    for u in (q for q in quotes if q["line"] == "umbrella"):
        req = {n["key"]: limit_numbers(n.get("text")) for n in u.get("notices", [])
               if n["key"] in ("underlying_auto_bi", "underlying_auto_pd") and n.get("check", {}).get("state") != "review"}
        req = {k: v for k, v in req.items() if v}
        if not req:
            continue
        ok = True
        for key, notice in (("bi_limit", "underlying_auto_bi"), ("pd_limit", "underlying_auto_pd")):
            need = req.get(notice)
            if not need:
                continue
            items = [it for it in auto.get("items", []) if it["key"] == key]
            haves = [limit_numbers(it.get("value")) for it in items]
            if not items or any(it["check"]["state"] == "review" for it in items) or not all(haves) \
                    or any(len(h) < len(need) or any(x < n for x, n in zip(h, need)) for h in haves):
                ok = False
        if ok:
            return display_carrier(u.get("carrier"))
    return None


# ---------------------------------------------------------------- tables

def build(comparison: dict) -> dict:
    cfg = lines_config()
    quotes = [dict(q, build_notices=[]) for q in comparison["quotes"]]
    kept = comparison.get("kept") or {}
    umbrella_requirement(quotes)
    tabs = []
    line_order = list(cfg["lines"]) + ["unknown"]
    for line in line_order:
        qs = [q for q in quotes if (q["line"] if q["line"] in cfg["lines"] else "unknown") == line]
        if qs:
            tabs.append(build_tab(line, qs, cfg, kept.get(line, [])))
    return {"tabs": tabs, "build_notices": {q["id"]: q["build_notices"] for q in quotes}}


def build_tab(line: str, qs: list[dict], cfg: dict, kept: list[str]) -> dict:
    qs = [dict(q, items=[dict(it, _idx=i) for i, it in enumerate(q.get("items", []))]) for q in qs]
    line_cfg = cfg["lines"].get(line) or {"label": "Other", "core": {}, "extras": {}}
    core, extras = line_cfg.get("core") or {}, line_cfg.get("extras") or {}
    columns = [{"quote_id": q["id"], "carrier": display_carrier(q.get("carrier")), "term_months": q.get("term_months"),
                "term_tag": {6: "6-mo", 12: "12-mo"}.get(q.get("term_months"), "term?"),
                "term_label": term_label(q), "file": q.get("file_name")} for q in qs]
    rows = []

    # price, with "Lowest" only when every price is trusted and there are at least two quotes
    comparable = {q["id"]: comparable_price(q) for q in qs}
    trusted = all(q["price"].get("check", {}).get("state") in ("found", "checked") for q in qs)
    lowest_id = None
    if len(qs) >= 2 and trusted and all(comparable.values()) and len({c[0] for c in comparable.values()}) == 1:
        lowest_id = min(comparable, key=lambda k: comparable[k][1])
    rows.append({"section": "price", "key": "price", "label": "Premium" if line == "life" else "Annual premium",
                 "cells": [price_cell(q, q["id"] == lowest_id) for q in qs]})

    if line in VEHICLE_ROW:
        rows.append({"section": "vehicles", "key": "vehicles", "label": VEHICLE_ROW[line], "cells": [
            {"lines": q.get("vehicles") or ["—"], "state": "found" if q.get("vehicles") else "not_listed",
             "reasons": [], "items": [], "note": None} for q in qs]})

    placed: dict[str, set[int]] = {q["id"]: set() for q in qs}

    def row(section: str, key: str, label: str, pick, multiple: bool = False) -> dict:
        cells = []
        per_vehicle = bool({**core, **extras}.get(key, {}).get("per_vehicle"))
        for q in qs:
            idx = [i for i, it in enumerate(q["items"]) if pick(it)]
            placed[q["id"]].update(idx)
            cells.append(items_cell(q, [q["items"][i] for i in idx], multiple, per_vehicle))
        return {"section": section, "key": key, "label": label, "cells": cells}

    if line != "unknown":
        for key, spec in core.items():
            rows.append(row("core", key, spec["label"], lambda it, k=key: it["key"] == k, spec.get("multiple")))
        important = {it["key"] for q in qs for it in q["items"] if it["importance"] == "key"}
        for key, spec in extras.items():
            if key in important or key in kept:
                rows.append(row("also", key, spec["label"], lambda it, k=key: it["key"] == k, spec.get("multiple")))
        for key, spec in extras.items():
            if key not in important and key not in kept and any(it["key"] == key for q in qs for it in q["items"]):
                rows.append(row("other", key, spec["label"], lambda it, k=key: it["key"] == k, spec.get("multiple")))

    # everything not placed yet: `other` items, grouped by label (case-insensitive exact match)
    labels: dict[str, str] = {}
    for q in qs:
        for i, it in enumerate(q["items"]):
            if i not in placed[q["id"]]:
                labels.setdefault((it.get("label") or "").strip().lower(), it.get("label") or "(no label)")
    for low, label in labels.items():
        key = f"other:{low}"
        section = "also" if key in kept else "other"
        rows.append(row(section, key, label, lambda it, lw=low: (it.get("label") or "").strip().lower() == lw
                        and it["key"] not in core and (it["key"] not in extras or line == "unknown")))

    notes = [good_to_know(q, cfg) for q in qs]
    rows.append({"section": "good_to_know", "key": "good_to_know", "label": "Good to know", "cells": [
        {"lines": [n["text"] for n in ns], "state": "found", "reasons": [], "items": ns, "note": None} for ns in notes]})

    order = {s: i for i, s in enumerate(SECTIONS)}
    rows.sort(key=lambda r: order[r["section"]])
    # the tab's "needs a check" dot counts only rows the broker can see; hidden rows are counted apart
    needs_check = any(c["state"] == "review" for r in rows if r["section"] != "other" for c in r["cells"])
    hidden = sum(1 for r in rows if r["section"] == "other" for c in r["cells"] if c["state"] == "review")
    return {"line": line, "label": line_cfg.get("label", line), "columns": columns, "rows": rows,
            "needs_check": needs_check, "hidden_needs_check": hidden}


def placement_count(tab: dict) -> dict[tuple, int]:
    """How many rows each (quote id, item index) landed in. Every item must land in exactly one."""
    counts: dict = {}
    for r in tab["rows"]:
        if r["section"] in ("price", "vehicles", "good_to_know"):
            continue
        for c in r["cells"]:
            for ref in c["items"]:
                k = (ref["quote_id"], ref["index"])
                counts[k] = counts.get(k, 0) + 1
    return counts

