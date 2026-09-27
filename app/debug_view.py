"""/debug/<comparison-id>: the hidden inspector (not linked from the broker's screen).

Per file: status and timings, each page's text and source next to the page image, the redacted
text with every replacement highlighted, the raw LLM output, and every value's check result.
Everything is escaped; the page runs no script.
"""
from __future__ import annotations

import json

from app import storage
from app.html_report import CSS, esc, table

STATE_CLASS = {"checked": "ok", "found": "", "review": "warn"}


def _redacted_html(p: dict) -> str:
    """The original page text with each replacement marked and its placeholder shown."""
    text, out, pos = p.get("original_text", p["text"]), [], 0
    for r in sorted(p.get("replacements", []), key=lambda r: r["start"]):
        out.append(esc(text[pos:r["start"]]))
        out.append(f'<mark title="{esc(r["kind"])}">{esc(r["original"])}</mark>'
                   f'<span class="muted">→{esc(r["placeholder"])}</span>')
        pos = r["end"]
    out.append(esc(text[pos:]))
    return "".join(out)


def _quote_html(q: dict) -> str:
    price = q.get("price") or {}
    pc = price.get("check", {})
    head = (f"<h3>{esc(q.get('carrier'))} · {esc(q.get('line'))} · {esc(q.get('term_months'))} months · id {esc(q['id'])}</h3>"
            f"<p>price <b>{esc(price.get('value'))}</b> ({esc(price.get('basis'))}, page {esc(price.get('page'))}) "
            f"<span class='{STATE_CLASS.get(pc.get('state'), '')}'>{esc(pc.get('state'))}</span> "
            f"{esc('; '.join(pc.get('reasons', [])))}<br><span class='muted'>{esc(price.get('source'))}</span></p>")
    sums = q.get("checks", {})
    head += "<p>" + " · ".join(f"{esc(k)}: {esc((v or {}).get('result'))} {esc({kk: vv for kk, vv in (v or {}).items() if kk != 'result'})}"
                                for k, v in sums.items()) + "</p>"
    rows, classes = [], []
    for i, it in enumerate(q.get("items", [])):
        c = it.get("check", {})
        rows.append([esc(i), esc(it["key"]) + (f"<br><span class='muted'>model: {esc(it['model_key'])}</span>" if it.get("model_key") else ""),
                     esc(it.get("label")), esc(it.get("vehicle")), f"<b>{esc(it.get('value'))}</b>", esc(it.get("premium")),
                     esc(it.get("importance")), esc(it.get("section")), esc(it.get("page")), esc(it.get("source")),
                     esc(c.get("state")), esc("; ".join(c.get("reasons", [])))])
        classes.append(STATE_CLASS.get(c.get("state"), ""))
    items = table(["#", "key", "label", "vehicle", "value", "premium", "importance", "section", "page", "source", "state", "reasons"],
                  rows, classes)
    notices = table(["key", "text", "page", "source", "state", "reasons"],
                    [[esc(n["key"]), esc(n.get("text")), esc(n.get("page")), esc(n.get("source")),
                      esc(n.get("check", {}).get("state")), esc("; ".join(n.get("check", {}).get("reasons", [])))]
                     for n in q.get("notices", [])] +
                    [[esc(n["key"]), esc(n.get("text")), "", "", "app", ""] for n in q.get("app_notices", [])])
    return head + items + "<h4>Notices</h4>" + notices


def render(cid: str) -> str:
    comp = storage.load(cid)
    parts = [f"<p>revision {comp['revision']} · updated {esc(comp['updated_at'])} · client {esc(comp['client']['name'])} · "
             f"{len(comp['files'])} files · {len(comp['quotes'])} quotes · {len(comp['edits'])} edits</p>"]
    if comp["edits"]:
        parts.append("<h2>Edits (applied on top of the quotes)</h2>" + table(
            ["at", "quote", "key", "index", "label", "vehicle", "action", "value"],
            [[esc(e["at"]), esc(e["quote_id"]), esc(e["key"]), esc(e.get("index")), esc(e.get("label")),
              esc(e.get("vehicle")), esc(e["action"]), esc(e.get("value"))] for e in comp["edits"]]))
    for f in comp["files"]:
        parts.append(f"<h2 id='{esc(f['id'])}'>{esc(f['name'])} <span class='muted'>({esc(f['id'])})</span></h2>")
        parts.append("<p>" + " · ".join(f"{esc(k)}: {esc(f.get(k))}" for k in
                                        ("status", "job_kind", "summary", "error", "error_detail", "redactions", "seconds",
                                         "model", "cache_key", "versions", "line_hints", "size", "sha256") if f.get(k) is not None) + "</p>")
        for q in (q for q in comp["quotes"] if q["file_id"] == f["id"]):
            parts.append(_quote_html(q))
        ext = storage.work_path(cid, f["id"], "extraction")
        if ext.exists():
            rec = storage.read_json(ext)
            parts.append(f"<details><summary>Raw LLM output ({esc(rec.get('meta', {}).get('model'))}, "
                         f"{esc(rec.get('meta', {}).get('input_tokens'))} in / {esc(rec.get('meta', {}).get('output_tokens'))} out, "
                         f"cache hit {esc(rec.get('meta', {}).get('cache_hit'))})</summary><pre>{esc(json.dumps(rec.get('raw'), indent=1))}</pre></details>")
        pages = storage.work_path(cid, f["id"], "pages")
        if pages.exists():
            for p in storage.read_json(pages):
                img = f"/api/comparisons/{esc(cid)}/pages/{esc(f['id'])}/{int(p['page'])}"
                parts.append(f"<div class='page'><img src='{img}' alt='page {int(p['page'])}' loading='lazy'>"
                             f"<div><b>page {int(p['page'])}</b> · source {esc(p['source'])} · {esc(round(p.get('seconds', 0), 2))}s · "
                             f"{esc(p.get('prep'))} · {len(p.get('replacements', []))} replacements"
                             f"<pre>{_redacted_html(p)}</pre></div></div>")
    title = f"Debug: {comp['client']['name']}"
    return (f"<!doctype html><meta charset='utf-8'><title>{esc(title)}</title><style>{CSS}</style>"
            f"<h1>{esc(title)}</h1>" + "\n".join(parts))
