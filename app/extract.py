"""Step 3: Extract. One LLM call per file -> quotes[] (JSON, schema enforced).

    python -m app.extract fixtures/X.pdf [--name "Client Name"] [--no-cache]
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time

import jsonschema
import yaml

from app import llm
from app.config import CONFIG_DIR, PROMPTS_DIR, ROOT, lines_config, settings

TOOL_NAME = "record_quotes"


class ExtractionFailed(RuntimeError):
    pass


def schema() -> dict:
    return json.loads((CONFIG_DIR / "extraction.schema.json").read_text(encoding="utf-8"))


def lines_config_for_prompt(cfg: dict | None = None) -> str:
    """lines.yaml as the LLM needs it: notice keys it may use, and every line's keys."""
    cfg = cfg or lines_config()
    notices = {k: v["desc"] for k, v in cfg["notice_keys"].items() if v.get("desc")}
    lines = {}
    for name, line in cfg["lines"].items():
        entry = {"label": line["label"]}
        if line.get("note"):
            entry["note"] = line["note"]
        for group in ("core", "extras"):
            entry[group] = {}
            for key, spec in (line.get(group) or {}).items():
                item = {"label": spec["label"], "desc": spec["desc"]}
                if spec.get("per_vehicle"):
                    item["per_vehicle"] = True
                entry[group][key] = item
        lines[name] = entry
    return yaml.safe_dump({"notice_keys": notices, "lines": lines}, sort_keys=False, allow_unicode=True,
                          width=120)


def system_prompt() -> str:
    text = (PROMPTS_DIR / "extract_system.md").read_text(encoding="utf-8")
    return (text.replace("{{LINES_CONFIG}}", lines_config_for_prompt())
            .replace("{{AGENCY_NAME}}", settings()["agency"]["name"]))


def user_message(pages: list[dict]) -> str:
    return "\n".join(f'<page n="{p["page"]}" source="{p["source"]}">\n{p["text"]}\n</page>' for p in pages)


def cache_key(system: str, user: str, schema_: dict | None = None, tag: str = "") -> str:
    """Hash of everything the model sees plus the versions and model settings.

    Keyed on the exact redacted input rather than only the file bytes, so a change to the reader
    or the redaction also re-runs extraction (grounding must check against what the model saw).
    """
    s = settings()
    parts = [user, system, json.dumps(schema_ or schema(), sort_keys=True),
             str(lines_config()["config_version"]), str(s["prompt_version"]), llm.generation_id()]
    if tag:  # experiments: a repeat of the same input gets its own cache entry (never sent to the model)
        parts.append(tag)
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def cache_dir():
    return ROOT / settings()["cache_dir"]


def validate(output: dict, schema_: dict | None = None) -> str | None:
    """Schema error message, or None if valid."""
    errors = sorted(jsonschema.Draft202012Validator(schema_ or schema()).iter_errors(output),
                    key=lambda e: list(e.path))
    if not errors:
        return None
    return "\n".join(f"- at {'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:10])


def normalize(output: dict, cfg: dict | None = None) -> dict:
    """Unknown keys become `other` (label kept); core items are always `key` importance."""
    cfg = cfg or lines_config()
    out = copy.deepcopy(output)
    for q in out["quotes"]:
        n = q.get("notices")
        if isinstance(n, dict):  # checklist form: one field per notice key, null when not stated
            lst = [{"key": k, **v} for k, v in n.items() if k != "other_notice" and v]
            lst += [{"key": "other_notice", **v} for v in n.get("other_notice") or []]
            q["notices"] = lst
        line = cfg["lines"].get(q["line"])
        core = set((line or {}).get("core") or {})
        extras = set((line or {}).get("extras") or {})
        for item in q["items"]:
            for f in ("value", "premium", "label", "vehicle"):  # a line break inside a value is layout, not content
                if isinstance(item.get(f), str):
                    item[f] = " ".join(item[f].split())
            if item["key"] not in core | extras:
                if item["key"] != "other":
                    item["model_key"] = item["key"]
                item["key"] = "other"
            if item["key"] in core:
                item["importance"] = "key"
        q.setdefault("vehicles", [])
        q.setdefault("premium_parts", None)
        q.setdefault("price_facts", None)
    return out


def _retry_delay(msg: str) -> float | None:
    """Seconds to wait before one retry, or None if this error should not be retried."""
    if "PerDay" in msg:
        return None
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate_limit" in msg:
        m = re.search(r"retry in ([\d.]+)s|retryDelay['\"]?:\s*['\"]?(\d+)", msg)
        return min((float(m.group(1) or m.group(2)) if m else 30.0) + 2, 90.0)  # never park the queue for long
    # Overload (503/529) is not retried: on the free tier a retry 30 s later almost always failed
    # too, so it only doubled the cost of a failure.
    return None


def _call(system: str, user, schema_: dict | None = None) -> llm.ToolCallResult:
    """One provider call, with at most one retry, and only on a per-minute rate limit.

    Every request counts against the provider's daily quota, including failed ones.
    """
    schema_ = schema_ or schema()
    try:
        return llm.call_tool(system, user, TOOL_NAME, schema_["description"], schema_)
    except llm.BudgetExceeded:
        raise
    except Exception as e:  # provider SDKs raise different classes
        delay = _retry_delay(str(e))
        if delay is None:
            raise
        print(f"  API error, one retry in {delay:.0f}s: {str(e)[:120]}", file=sys.stderr)
        time.sleep(delay)
        return llm.call_tool(system, user, TOOL_NAME, schema_["description"], schema_)


def extract(pages: list[dict], use_cache: bool = True, cache_only: bool = False, hint: str | None = None) -> dict:
    """The pipeline's extraction: redacted page text in, {"quotes": [...], "meta": {...}} out.
    Raises ExtractionFailed after the one retry.

    cache_only: never call the API; raise ExtractionFailed if there is no cached result.
    hint: a correction from the broker (e.g. which line a quote is for), sent after the pages."""
    user = user_message(pages)
    if hint:
        user += f"\n<broker_correction>\n{hint}\n</broker_correction>"
    return extract_content(system_prompt(), user, use_cache=use_cache, cache_only=cache_only)


def extract_content(system: str, user: str, schema_: dict | None = None, use_cache: bool = True,
                    cache_only: bool = False, normalize_fn=None, cache_tag: str = "") -> dict:
    """One forced tool call with any system prompt, redacted text input, and
    schema; validated, retried once on a schema error, normalized, and cached."""
    schema_ = schema_ or schema()
    key = cache_key(system, user, schema_, cache_tag)
    path = cache_dir() / f"{key}.json"
    cached = None
    if (use_cache or cache_only) and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            cached["meta"]["cache_hit"] = True
            cached["raw"]
        except (ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)  # a damaged entry is a miss, never a file that fails forever
            cached = None
    if cached is not None:
        cached["quotes"] = (normalize_fn or normalize)(cached["raw"])["quotes"]  # normalizing is code: always current
        return cached
    if cache_only:
        raise ExtractionFailed("not in cache")

    calls, message = [], user
    output = None
    for attempt in (1, 2):
        try:
            result = _call(system, message, schema_)
        except llm.BudgetExceeded:
            raise
        except Exception as e:
            raise ExtractionFailed(f"LLM call failed: {str(e)[:200]}") from e
        calls.append({"seconds": round(result.seconds, 2), "input_tokens": result.input_tokens,
                      "output_tokens": result.output_tokens, "stop_reason": result.stop_reason})
        error = "You did not call the record_quotes tool." if result.args is None else validate(result.args, schema_)
        if error is None:
            output = result.args
            break
        calls[-1]["error"] = error
        retry_note = (f"\n\nYour previous answer did not match the record_quotes schema:\n{error}\n"
                      "Call record_quotes again with a complete, corrected answer.")
        message = user + retry_note
    meta = {"provider": result.provider, "model": result.model, "calls": calls, "cache_key": key,
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "seconds": round(sum(c["seconds"] for c in calls), 2),
            "prompt_version": settings()["prompt_version"],
            "config_version": lines_config()["config_version"], "cache_hit": False}
    if output is None:
        raise ExtractionFailed(f"extraction failed after retry: {calls[-1].get('error')}")
    record = {"raw": output, "quotes": (normalize_fn or normalize)(output)["quotes"], "meta": meta}
    from app.storage import atomic_write  # a crash mid-write must not leave a damaged entry
    atomic_write(path, json.dumps(record, indent=1).encode("utf-8"))
    return record


def main(argv: list[str] | None = None) -> None:
    from app.read import read_file
    from app.redact import Client, redact_pages
    ap = argparse.ArgumentParser(description="Extract quotes from one file.")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--name", default="")
    ap.add_argument("--address", default="")
    ap.add_argument("--other", default="")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--print-prompt", action="store_true")
    args = ap.parse_args(argv)
    if args.print_prompt:
        print(system_prompt())
        return
    client = Client(args.name, args.address, [n.strip() for n in args.other.split(",") if n.strip()])
    pages, _ = redact_pages(read_file(args.file), client)
    rec = extract(pages, use_cache=not args.no_cache)
    print(json.dumps({"quotes": rec["quotes"], "meta": rec["meta"]}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
