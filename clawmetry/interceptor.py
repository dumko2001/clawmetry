"""
ClawMetry HTTP Interceptor — zero-config LLM cost tracking.

Monkey-patches httpx and requests to intercept LLM API calls.
Detects provider from hostname, extracts token counts from responses,
tracks costs locally, and prints a summary on session exit.

Usage:
    import clawmetry  # that's it

Local mode (default): terminal output + local ledger, zero network calls.
Cloud mode (opt-in): set CLAWMETRY_API_KEY env var to also sync to dashboard.
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
from typing import Optional

from clawmetry.providers_pricing import PROVIDER_MAP, estimate_cost_usd

# ── Thread-safe ledger ────────────────────────────────────────────────────────

_lock = threading.Lock()

_ledger = {
    "session_start": time.time(),
    "calls": 0,
    "cost_usd": 0.0,
    "tokens_in": 0,
    "tokens_out": 0,
    "providers": {},  # provider_name -> {"calls": int, "cost_usd": float}
}

_patched = {
    "httpx": False,
    "requests": False,
}


def _record(provider: str, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    """Record a call into the ledger (thread-safe)."""
    with _lock:
        _ledger["calls"] += 1
        _ledger["cost_usd"] += cost_usd
        _ledger["tokens_in"] += tokens_in
        _ledger["tokens_out"] += tokens_out
        p = _ledger["providers"].setdefault(provider, {"calls": 0, "cost_usd": 0.0})
        p["calls"] += 1
        p["cost_usd"] += cost_usd


def _detect_provider(url: str) -> Optional[str]:
    """Detect LLM provider name from a URL string."""
    for hostname, info in PROVIDER_MAP.items():
        if hostname in url:
            return info["name"]
    return None


def _extract_usage(response_body: bytes, provider: str) -> tuple[int, int]:
    """Extract (tokens_in, tokens_out) from a response body JSON blob."""
    try:
        data = json.loads(response_body)
    except Exception:
        return 0, 0

    usage = data.get("usage") or {}

    # Anthropic: input_tokens, output_tokens
    tokens_in = (
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or 0
    )
    tokens_out = (
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or 0
    )

    # Gemini: usageMetadata.promptTokenCount / candidatesTokenCount
    if tokens_in == 0 and tokens_out == 0:
        meta = data.get("usageMetadata") or {}
        tokens_in = meta.get("promptTokenCount") or 0
        tokens_out = meta.get("candidatesTokenCount") or 0

    return int(tokens_in), int(tokens_out)


# ── Daily ledger file ─────────────────────────────────────────────────────────

def _ledger_path() -> str:
    d = os.path.expanduser("~/.clawmetry")
    os.makedirs(d, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    return os.path.join(d, f"ledger-{day}.json")


def _load_daily_cost() -> float:
    try:
        with open(_ledger_path()) as f:
            data = json.load(f)
        return float(data.get("cost_usd", 0.0))
    except Exception:
        return 0.0


def _save_daily_cost(session_cost: float) -> None:
    path = _ledger_path()
    existing = _load_daily_cost()
    total = existing + session_cost
    try:
        with open(path, "w") as f:
            json.dump({"cost_usd": total, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
    except Exception:
        pass


# ── Exit summary ──────────────────────────────────────────────────────────────

def _print_summary() -> None:
    with _lock:
        calls = _ledger["calls"]
        cost = _ledger["cost_usd"]
        elapsed = time.time() - _ledger["session_start"]
        providers = dict(_ledger["providers"])

    if calls == 0:
        return  # nothing tracked — stay silent

    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    # provider breakdown
    breakdown_parts = []
    for pname, pdata in sorted(providers.items(), key=lambda x: -x[1]["cost_usd"]):
        breakdown_parts.append(f"{pname}: ${pdata['cost_usd']:.2f}")
    breakdown = " · ".join(breakdown_parts) if breakdown_parts else ""

    daily = _load_daily_cost() + cost
    # Rough monthly projection from daily spend
    # (Save AFTER reading daily to get today's total)
    _save_daily_cost(cost)
    daily_total = _load_daily_cost()
    monthly = daily_total * 30

    print(
        f"\nclawmetry ▸ session: ${cost:.2f} ({calls} call{'s' if calls != 1 else ''}, {elapsed_str})"
        f" ── today: ${daily_total:.2f} ── ~${monthly:.0f}/mo"
    )
    if breakdown:
        print(f"           {breakdown}")


atexit.register(_print_summary)


# ── httpx patching ────────────────────────────────────────────────────────────

def _patch_httpx() -> None:
    try:
        import httpx
    except ImportError:
        return

    if _patched["httpx"]:
        return

    _orig_send = httpx.Client.send
    _orig_send_async = None

    def _patched_send(self, request, *args, **kwargs):  # type: ignore[override]
        response = _orig_send(self, request, *args, **kwargs)
        _handle_response_sync(str(request.url), response.content)
        return response

    httpx.Client.send = _patched_send  # type: ignore[method-assign]

    # Async client
    try:
        import asyncio

        _orig_async_send = httpx.AsyncClient.send

        async def _patched_async_send(self, request, *args, **kwargs):  # type: ignore[override]
            response = await _orig_async_send(self, request, *args, **kwargs)
            _handle_response_sync(str(request.url), response.content)
            return response

        httpx.AsyncClient.send = _patched_async_send  # type: ignore[method-assign]
    except Exception:
        pass

    _patched["httpx"] = True


def _handle_response_sync(url: str, body: bytes) -> None:
    """Process a response body after an HTTP call (shared by all patches)."""
    provider = _detect_provider(url)
    if provider is None:
        return
    tokens_in, tokens_out = _extract_usage(body, provider)
    if tokens_in == 0 and tokens_out == 0:
        return
    cost = estimate_cost_usd(provider, tokens_in, tokens_out)
    _record(provider, tokens_in, tokens_out, cost)


# ── requests patching ─────────────────────────────────────────────────────────

def _patch_requests() -> None:
    try:
        import requests
    except ImportError:
        return

    if _patched["requests"]:
        return

    _orig_send = requests.Session.send

    def _patched_send(self, request, *args, **kwargs):  # type: ignore[override]
        response = _orig_send(self, request, *args, **kwargs)
        try:
            _handle_response_sync(request.url or "", response.content)
        except Exception:
            pass
        return response

    requests.Session.send = _patched_send  # type: ignore[method-assign]
    _patched["requests"] = True


# ── urllib patching ───────────────────────────────────────────────────────────

def _patch_urllib() -> None:
    try:
        import urllib.request
    except ImportError:
        return

    _orig_urlopen = urllib.request.urlopen

    def _patched_urlopen(url, data=None, *args, **kwargs):
        response = _orig_urlopen(url, data, *args, **kwargs)
        # urllib responses are not buffered — wrap read to intercept
        # Only track POST calls (all LLM API calls are POST)
        try:
            url_str = url if isinstance(url, str) else getattr(url, "full_url", str(url))
            provider = _detect_provider(url_str)
            if provider and data is not None:
                _orig_read = response.read
                _buf = []

                def _intercepted_read(*a):
                    chunk = _orig_read(*a)
                    _buf.append(chunk)
                    return chunk

                response.read = _intercepted_read  # type: ignore[method-assign]
                # Register cleanup so we process after caller reads
                _orig_close = response.close

                def _intercepted_close():
                    body = b"".join(_buf)
                    _handle_response_sync(url_str, body)
                    _orig_close()

                response.close = _intercepted_close  # type: ignore[method-assign]
        except Exception:
            pass
        return response

    urllib.request.urlopen = _patched_urlopen


def patch_all() -> None:
    """Patch all supported HTTP libraries. Safe to call multiple times."""
    _patch_httpx()
    _patch_requests()
    _patch_urllib()


def get_session_stats() -> dict:
    """Return current session stats (copy)."""
    with _lock:
        return {
            "calls": _ledger["calls"],
            "cost_usd": round(_ledger["cost_usd"], 6),
            "tokens_in": _ledger["tokens_in"],
            "tokens_out": _ledger["tokens_out"],
            "providers": {
                k: dict(v) for k, v in _ledger["providers"].items()
            },
        }
