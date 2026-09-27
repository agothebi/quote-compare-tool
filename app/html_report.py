"""Tiny helpers for the plain static HTML milestone reports."""
from __future__ import annotations

import html
import os
from pathlib import Path

from app.config import REPORTS_DIR

CSS = """
body{font-family:sans-serif;font-size:13px;margin:16px}
table{border-collapse:collapse;margin:8px 0}
td,th{border:1px solid #ccc;padding:3px 6px;vertical-align:top;text-align:left}
pre{white-space:pre-wrap;font-size:12px;margin:0}
.page{display:flex;gap:12px;margin:10px 0;border-top:2px solid #999;padding-top:8px}
.page img{width:560px;border:1px solid #ccc}
.page pre{flex:1}
.ok{background:#d9f5d9}.bad{background:#fbd4d4}.warn{background:#fff3c4}.muted{color:#888}
mark{background:#ffe066}
.cols{display:flex;gap:12px}.cols>div{flex:1}
"""


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def rel(path: str | Path) -> str:
    return os.path.relpath(Path(path).resolve(), REPORTS_DIR.resolve())


def table(headers: list[str], rows: list[list], row_classes: list[str] | None = None) -> str:
    out = ["<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"]
    for i, r in enumerate(rows):
        cls = f' class="{row_classes[i]}"' if row_classes and row_classes[i] else ""
        out.append(f"<tr{cls}>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def write(name: str, title: str, body: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / name
    path.write_text(f"<!doctype html><meta charset='utf-8'><title>{esc(title)}</title>"
                    f"<style>{CSS}</style><h1>{esc(title)}</h1>\n{body}", encoding="utf-8")
    return path
