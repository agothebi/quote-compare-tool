"""One forced tool call to the configured LLM provider.

Both providers get the same system prompt, the same user text, and the same JSON schema as
the only tool, and must answer by calling it. Switching model is one line: `llm.model` in
config/settings.yaml, or QUOTE_COMPARE_MODEL in .env on one computer. The provider follows from the
pricing table the model is listed in; nothing else in the pipeline knows which one ran.

    python -m app.llm        # check the key and list models for the configured provider
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import DATA_DIR, load_env, settings

USAGE_FILE = DATA_DIR / "llm_usage.json"
# Provider daily quotas reset at midnight Pacific time.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    """The daily request budget is used up. Stop the run; retrying only wastes requests."""


class LedgerDamaged(BudgetExceeded):
    """data/llm_usage.json can't be read, so nothing is spent until it is restored or deleted."""


@dataclass
class ToolCallResult:
    args: dict | None          # the tool arguments, or None if the model did not call the tool
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float
    stop_reason: str | None
    cached_input_tokens: int = 0  # part of input_tokens read from the provider's prompt cache (cheaper)
    cache_write_tokens: int = 0   # part of input_tokens written to the prompt cache (Anthropic)


_OVERRIDE: dict = {}


def override(**kw) -> None:
    """Temporarily use another model or thinking level (benchmarks). override() clears it."""
    _OVERRIDE.clear()
    _OVERRIDE.update(kw)


PROVIDERS = ("gemini", "anthropic")
MODEL_ENV = "QUOTE_COMPARE_MODEL"  # in .env: this computer's model, kept when the app is updated


def llm_settings() -> dict:
    load_env()
    cfg = settings()["llm"]
    over = {k: v for k, v in _OVERRIDE.items() if v is not None}
    model = over.get("model") or (os.environ.get(MODEL_ENV) or "").strip() or cfg.get("model")
    provider = next((p for p in PROVIDERS if model in ((cfg.get(p) or {}).get("pricing") or {})), None)
    if provider is None:
        known = ", ".join(m for p in PROVIDERS for m in ((cfg.get(p) or {}).get("pricing") or {}))
        raise LLMError(f"Unknown model {model!r}. Use one of: {known}. "
                       "(A new model needs its price added under pricing in config/settings.yaml.)")
    return {"provider": provider, **cfg[provider], **over, "model": model,
            "temperature": cfg.get("temperature"), "max_output_tokens": cfg["max_output_tokens"],
            "spend_limit_usd_total": cfg.get("spend_limit_usd_total"),
            "spend_limit_usd_per_month": cfg.get("spend_limit_usd_per_month"),
            "spend_limit_usd_per_day": cfg.get("spend_limit_usd_per_day"),
            "request_timeout_seconds": cfg.get("request_timeout_seconds") or 180}


def price(s: dict) -> dict | None:
    """{'input': usd, 'output': usd} per 1M tokens for the configured model, or None if unknown."""
    return (s.get("pricing") or {}).get(s["model"])


def cost_usd(s: dict, input_tokens: int, output_tokens: int, cached: int = 0, written: int = 0) -> float:
    """Cost of one call. `cached` and `written` are the parts of `input_tokens` read from and written
    to the prompt cache; without a configured cache price they cost the full input price (an
    over-estimate, the safe side for the spend caps)."""
    p = price(s)
    if p is None:
        return 0.0
    plain = max(input_tokens - cached - written, 0)
    return (plain * p["input"] + cached * p.get("cached_input", p["input"])
            + written * p.get("cache_write", p["input"]) + output_tokens * p["output"]) / 1e6


def model_id() -> str:
    """'provider/model', for display."""
    s = llm_settings()
    return f"{s['provider']}/{s['model']}"


def generation_id() -> str:
    """Model plus every setting that changes its answer; part of the extraction cache key."""
    s = llm_settings()
    gen = {k: s.get(k) for k in ("temperature", "max_output_tokens", "thinking_level", "thinking_budget")}
    return f"{s['provider']}/{s['model']} {json.dumps(gen, sort_keys=True)}"


def _api_key(s: dict) -> str:
    load_env()
    key = os.environ.get(s["api_key_env"])
    if not key:
        raise LLMError(f"{s['api_key_env']} is not set (add it to .env or the environment)")
    return key


# ---------------------------------------------------------------- request budget

def _load_usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:  # never read as "nothing spent": the caps would reset
        raise LedgerDamaged(f"{USAGE_FILE} is damaged, so spending is paused (restore or delete it)") from e


def _save_usage(usage: dict) -> None:
    from app.storage import atomic_write  # a crash mid-write must not wipe the spend ledger
    atomic_write(USAGE_FILE, json.dumps(usage, indent=1).encode())


def _today() -> str:
    return datetime.now(QUOTA_TZ).strftime("%Y-%m-%d")


def requests_today(model: str | None = None) -> dict:
    """{'ok': n, 'error': n, 'exhausted': bool} for this key and model today (Pacific date)."""
    day = _load_usage().get(_today(), {})
    return day.get(model or budget_id(), {"ok": 0, "error": 0, "exhausted": False})


def _record(model: str, ok: bool, exhausted: bool = False, result: "ToolCallResult | None" = None,
            cost: float = 0.0) -> None:
    usage = _load_usage()
    entry = usage.setdefault(_today(), {}).setdefault(model, {"ok": 0, "error": 0, "exhausted": False})
    entry["ok" if ok else "error"] += 1
    entry["exhausted"] = entry["exhausted"] or exhausted
    entry["last_request"] = time.time()
    if result is not None:
        entry["input_tokens"] = entry.get("input_tokens", 0) + result.input_tokens
        entry["cached_input_tokens"] = entry.get("cached_input_tokens", 0) + result.cached_input_tokens
        entry["output_tokens"] = entry.get("output_tokens", 0) + result.output_tokens
        entry["max_output_tokens_seen"] = max(entry.get("max_output_tokens_seen", 0), result.output_tokens)
    entry["cost_usd"] = round(entry.get("cost_usd", 0.0) + cost, 6)
    _save_usage(usage)


def spent_usd(provider: str, day: str | None = None, month: str | None = None) -> float:
    """Logged spend for a provider: all days, one day ('2026-09-27'), or one month ('2026-09')."""
    total = 0.0
    for d, models in _load_usage().items():
        if (day and d != day) or (month and not d.startswith(month)):
            continue
        total += sum(e.get("cost_usd", 0.0) for m, e in models.items() if m.startswith(provider + "/"))
    return total


def budget_id(s: dict | None = None) -> str:
    """Quotas are per API key's project and per model, so the budget is tracked per key and model.

    Only a short hash of the key is stored."""
    import hashlib
    s = s or llm_settings()
    fp = hashlib.sha256(_api_key(s).encode()).hexdigest()[:8]
    return f"{s['provider']}/{s['model']}@key-{fp}"


def max_output_seen(s: dict) -> int:
    """Largest output (incl. thinking) logged for this provider/model on any day."""
    prefix = f"{s['provider']}/{s['model']}@"
    return max((e.get("max_output_tokens_seen", 0) for day in _load_usage().values()
                for m, e in day.items() if m.startswith(prefix)), default=0)


def estimate_input_tokens(system: str, user: str) -> int:
    """A generous estimate (3 characters a token) for the spend check before a call."""
    return (len(system) + len(user)) // 3 + 500


def worst_case_usd(s: dict, system: str, user) -> float:
    """Upper bound for one call: generous input estimate (3 chars a token), and output of the larger
    of max_output_tokens and 1.5x the most ever seen. Gemini 3 thinking tokens are not limited by
    max_output_tokens (17,799 were seen with a 16,000 cap), so the cap alone is not an upper bound."""
    out = max(s["max_output_tokens"], int(1.5 * max_output_seen(s)))
    return cost_usd(s, estimate_input_tokens(system, user), out)


def _check_spend(s: dict, system: str, user: str) -> None:
    caps = (s.get("spend_limit_usd_total"), s.get("spend_limit_usd_per_day"), s.get("spend_limit_usd_per_month"))
    if all(c is None for c in caps):
        return
    if price(s) is None:
        raise BudgetExceeded(f"no price configured for {s['model']} (llm.{s['provider']}.pricing); "
                             "refusing to call without a spend estimate")
    worst = worst_case_usd(s, system, user)
    total, today = spent_usd(s["provider"]), spent_usd(s["provider"], _today())
    if caps[0] is not None and total + worst > caps[0]:
        raise BudgetExceeded(f"spend cap: ${total:.4f} spent in total, this call could add ${worst:.4f}, "
                             f"cap ${caps[0]:.2f} (llm.spend_limit_usd_total)")
    if caps[1] is not None and today + worst > caps[1]:
        raise BudgetExceeded(f"daily spend cap: ${today:.4f} spent today, this call could add ${worst:.4f}, "
                             f"cap ${caps[1]:.2f} (llm.spend_limit_usd_per_day)")
    month = _today()[:7]
    this_month = spent_usd(s["provider"], month=month)
    if caps[2] is not None and this_month + worst > caps[2]:
        raise BudgetExceeded(f"monthly spend cap: ${this_month:.4f} spent in {month}, this call could add "
                             f"${worst:.4f}, cap ${caps[2]:.2f} (llm.spend_limit_usd_per_month)")


def _check_budget_and_pace(s: dict) -> None:
    """Refuse to send when today's budget is used up; space requests to stay under per-minute limits."""
    model = budget_id(s)
    used = requests_today(model)
    limit = s.get("daily_request_limit")
    if used.get("exhausted"):
        raise BudgetExceeded(f"{model}: the provider reported today's quota as used up")
    if limit is not None and used["ok"] + used["error"] >= limit:
        raise BudgetExceeded(f"{model}: {used['ok'] + used['error']} of {limit} requests used today "
                             "(llm.<provider>.daily_request_limit)")
    gap = s.get("min_seconds_between_requests") or 0
    wait = used.get("last_request", 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)


def call_tool(system: str, user: str, tool_name: str, tool_description: str,
              schema: dict) -> ToolCallResult:
    """Exactly one HTTP request (SDK retries are off), counted against the daily budget.
    `user` is text only: page images and PDFs are never sent (only redacted text leaves the computer)."""
    if not isinstance(user, str):
        raise LLMError("only text can be sent to the model")
    s = llm_settings()
    _check_spend(s, system, user)
    _check_budget_and_pace(s)
    model = budget_id(s)
    try:
        if s["provider"] == "gemini":
            result = _call_gemini(s, system, user, tool_name, tool_description, schema)
        else:
            result = _call_anthropic(s, system, user, tool_name, tool_description, schema)
    except Exception as e:
        daily = "PerDay" in str(e)
        _record(model, ok=False, exhausted=daily)
        if daily:
            raise BudgetExceeded(f"{model}: provider says today's quota is used up") from e
        raise
    _record(model, ok=True, result=result, cost=cost_usd(s, result.input_tokens, result.output_tokens,
                                                         result.cached_input_tokens, result.cache_write_tokens))
    return result


def failure_kind(e: BaseException | None) -> str:
    """What a failed provider call means for the broker: 'key' (the key was refused), 'model' (the
    configured model is retired or unknown), 'provider' (the provider's own trouble), or 'network'.

    Both SDKs carry the HTTP status: Anthropic as `status_code`, google-genai as `code`."""
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    text = str(e).lower()
    if status in (401, 403) or any(w in text for w in ("api key not valid", "api_key_invalid", "invalid x-api-key",
                                                       "permission_denied", "unauthenticated", "authentication")):
        return "key"
    if status == 404 or ("model" in text and ("not found" in text or "not_found" in text)):
        return "model"
    if isinstance(status, int) and (status >= 500 or status in (429, 529)):
        return "provider"
    return "network"


# ---------------------------------------------------------------- Gemini

def _gemini_schema(schema: dict) -> dict:
    out = copy.deepcopy(schema)
    for k in ("$schema", "title"):
        out.pop(k, None)
    return out


def _call_gemini(s, system, user, tool_name, tool_description, schema) -> ToolCallResult:
    from google import genai
    from google.genai import types

    # attempts=1: the SDK otherwise retries 429/5xx up to 5 times, silently spending requests
    client = genai.Client(api_key=_api_key(s), http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1),
        timeout=int(s["request_timeout_seconds"] * 1000)))  # a hung call must not block the worker forever
    tool = types.Tool(function_declarations=[types.FunctionDeclaration(
        name=tool_name, description=tool_description,
        parameters_json_schema=_gemini_schema(schema))])
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=s["temperature"],
        max_output_tokens=s["max_output_tokens"],
        tools=[tool],
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY, allowed_function_names=[tool_name])),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=(types.ThinkingConfig(thinking_level=s["thinking_level"].upper())
                         if s.get("thinking_level") else
                         types.ThinkingConfig(thinking_budget=s["thinking_budget"])
                         if s.get("thinking_budget") is not None else None),
    )
    t0 = time.perf_counter()
    resp = client.models.generate_content(model=s["model"], contents=user, config=config)
    seconds = time.perf_counter() - t0
    calls = [c for c in (resp.function_calls or []) if c.name == tool_name]
    usage = resp.usage_metadata
    finish = resp.candidates[0].finish_reason if resp.candidates else None
    return ToolCallResult(
        args=dict(calls[0].args) if calls else None,
        provider="gemini", model=s["model"],
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        # thinking tokens are billed as output
        output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
        cached_input_tokens=(getattr(usage, "cached_content_token_count", None) or 0) if usage else 0,
        seconds=seconds, stop_reason=str(finish) if finish else None)


# ---------------------------------------------------------------- Anthropic

def _call_anthropic(s, system, user, tool_name, tool_description, schema) -> ToolCallResult:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(s), max_retries=0,  # retries are decided by the caller
                                 timeout=float(s["request_timeout_seconds"]))
    kwargs = {}
    # Claude Sonnet 5 / Opus 5 reject sampling parameters; older models accept temperature.
    if s["temperature"] is not None and not s["model"].startswith(("claude-sonnet-5", "claude-opus-5")):
        kwargs["temperature"] = s["temperature"]
    t0 = time.perf_counter()
    resp = client.messages.create(
        model=s["model"],
        max_tokens=s["max_output_tokens"],
        # the system prompt (with the tool schema before it) is the same for every file: cache it,
        # so the files after the first in a batch pay about a tenth for those ~10k tokens
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=[{"name": tool_name, "description": tool_description, "input_schema": schema}],
        tool_choice={"type": "tool", "name": tool_name},
        # forced tool choice does not combine with thinking
        thinking={"type": "disabled"},
        **kwargs,
    )
    seconds = time.perf_counter() - t0
    blocks = [b for b in resp.content if b.type == "tool_use" and b.name == tool_name]
    return ToolCallResult(
        args=blocks[0].input if blocks else None,
        provider="anthropic", model=s["model"],
        input_tokens=(resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0)
                      + (resp.usage.cache_creation_input_tokens or 0)),
        output_tokens=resp.usage.output_tokens,
        cached_input_tokens=resp.usage.cache_read_input_tokens or 0,
        cache_write_tokens=resp.usage.cache_creation_input_tokens or 0,
        seconds=seconds, stop_reason=resp.stop_reason)


def print_usage() -> None:
    try:
        usage = _load_usage()
    except LedgerDamaged as e:
        print(e)
        return
    print(f"{'day':10}  {'model':44} {'ok':>4} {'err':>4} {'in tok':>9} {'out tok':>9} {'cost $':>9}")
    for day, models in sorted(usage.items()):
        for m, e in models.items():
            print(f"{day:10}  {m:44} {e['ok']:4} {e['error']:4} {e.get('input_tokens', 0):9} "
                  f"{e.get('output_tokens', 0):9} {e.get('cost_usd', 0):9.4f}")
    s = llm_settings()
    print(f"\n{s['provider']} spend: ${spent_usd(s['provider']):.4f} total "
          f"(this month ${spent_usd(s['provider'], month=_today()[:7]):.4f}, cap ${s.get('spend_limit_usd_per_month')}), ${spent_usd(s['provider'], _today()):.4f} today "
          f"(cap ${s.get('spend_limit_usd_per_day')})")


def main() -> None:
    if "--usage" in sys.argv:
        print_usage()
        return
    s = llm_settings()
    print(f"provider: {s['provider']}  model: {s['model']}  key: {s['api_key_env']}")
    key = _api_key(s)
    if s["provider"] == "gemini":
        from google import genai
        client = genai.Client(api_key=key)  # keep a reference: pagination needs the open client
        names = [m.name for m in client.models.list()
                 if "generateContent" in (m.supported_actions or [])]
    else:
        import anthropic
        names = [m.id for m in anthropic.Anthropic(api_key=key).models.list()]
    print("available models:", ", ".join(sorted(names)))
    found = any(n.split("/")[-1] == s["model"] for n in names)
    print("configured model is available" if found else "WARNING: configured model not in the list")
    sys.exit(0 if found else 1)


if __name__ == "__main__":
    main()
