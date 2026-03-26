"""
clawmetry/providers.py — Provider detection, pricing table, and usage extraction.

Prices are per million tokens, as of March 2026.
"""
from __future__ import annotations

from typing import Optional, Dict, Any

PRICING = {
    "anthropic": {
        "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
        "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
        "claude-opus-4": {"input": 15.0, "output": 75.0},
        "claude-sonnet-4": {"input": 3.0, "output": 15.0},
        "default": {"input": 3.0, "output": 15.0},
    },
    "openai": {
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "default": {"input": 2.5, "output": 10.0},
    },
    "gemini": {
        "gemini-1.5-pro": {"input": 1.25, "output": 5.0},
        "gemini-1.5-flash": {"input": 0.075, "output": 0.3},
        "default": {"input": 1.25, "output": 5.0},
    },
    "mistral": {"default": {"input": 2.0, "output": 6.0}},
    "groq": {"default": {"input": 0.05, "output": 0.08}},
    "together": {"default": {"input": 0.2, "output": 0.2}},
    "deepseek": {"default": {"input": 0.14, "output": 0.28}},
    "cohere": {"default": {"input": 0.15, "output": 0.6}},
}


def _fuzzy_match_model(provider: str, model: str) -> str:
    """
    Return the best matching pricing key for *model* within *provider*.

    Strategy: prefer the longest pricing key that is a substring of the
    model string (e.g. "claude-3-5-sonnet-20241022" matches
    "claude-3-5-sonnet").  Falls back to "default".
    """
    if provider not in PRICING:
        return "default"

    model_lower = model.lower() if model else ""
    provider_models = PRICING[provider]

    best_key = "default"
    best_len = 0
    for key in provider_models:
        if key == "default":
            continue
        if key in model_lower and len(key) > best_len:
            best_key = key
            best_len = len(key)

    return best_key


def get_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Return the estimated cost in USD for a single API call.

    Args:
        provider: Provider name, e.g. "anthropic", "openai".
        model: Model identifier as returned by the API.
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/completion tokens.

    Returns:
        Cost in USD (float, may be 0.0 if provider unknown).
    """
    if provider not in PRICING:
        return 0.0

    key = _fuzzy_match_model(provider, model)
    prices = PRICING[provider].get(key) or PRICING[provider].get("default", {})

    input_cost = (input_tokens / 1_000_000) * prices.get("input", 0.0)
    output_cost = (output_tokens / 1_000_000) * prices.get("output", 0.0)
    return input_cost + output_cost


# ──────────────────────────────────────────────────────────────────────────────
# Provider detection from URL/hostname
# ──────────────────────────────────────────────────────────────────────────────

# Maps hostname substrings → canonical provider name
_HOST_MAP: list[tuple[str, str]] = [
    ("api.anthropic.com", "anthropic"),
    ("anthropic.com", "anthropic"),
    ("api.openai.com", "openai"),
    ("openai.azure.com", "openai"),
    ("generativelanguage.googleapis.com", "gemini"),
    ("aiplatform.googleapis.com", "gemini"),
    ("api.mistral.ai", "mistral"),
    ("api.groq.com", "groq"),
    ("api.together.xyz", "together"),
    ("api.together.ai", "together"),
    ("api.deepseek.com", "deepseek"),
    ("api.cohere.ai", "cohere"),
    ("api.cohere.com", "cohere"),
]


def detect_provider(url: str) -> Optional[str]:
    """
    Return the provider name for *url*, or None if not a known LLM endpoint.

    Fast linear scan over a small table — sub-microsecond in practice.
    Never raises.
    """
    try:
        url_lower = url.lower()
        for host, provider in _HOST_MAP:
            if host in url_lower:
                return provider
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Usage extraction from response JSON
# ──────────────────────────────────────────────────────────────────────────────

def extract_usage(provider: str, body: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse token counts and model name from a raw response body.

    Returns a dict with keys: model, input_tokens, output_tokens.
    Returns None if parsing fails or no usage data found.
    Never raises.
    """
    try:
        import json

        data = json.loads(body)

        if provider == "anthropic":
            return _extract_anthropic(data)
        elif provider == "openai":
            return _extract_openai(data)
        elif provider == "gemini":
            return _extract_gemini(data)
        elif provider in ("mistral", "groq", "together", "deepseek", "cohere"):
            # All use OpenAI-compatible format
            return _extract_openai(data)
        else:
            # Generic fallback: try both formats
            result = _extract_openai(data)
            if result:
                return result
            return _extract_anthropic(data)
    except Exception:
        return None


def _extract_anthropic(data: dict) -> Optional[Dict[str, Any]]:
    """Anthropic Messages API response shape."""
    try:
        usage = data.get("usage", {})
        input_tokens = int(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or 0
        )
        output_tokens = int(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or 0
        )
        if input_tokens == 0 and output_tokens == 0:
            return None
        return {
            "model": data.get("model", ""),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    except Exception:
        return None


def _extract_openai(data: dict) -> Optional[Dict[str, Any]]:
    """OpenAI Chat Completions API (and compatible) response shape."""
    try:
        usage = data.get("usage", {})
        input_tokens = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        output_tokens = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        if input_tokens == 0 and output_tokens == 0:
            return None
        return {
            "model": data.get("model", ""),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    except Exception:
        return None


def _extract_gemini(data: dict) -> Optional[Dict[str, Any]]:
    """Google Gemini generateContent response shape."""
    try:
        meta = data.get("usageMetadata", {})
        input_tokens = int(meta.get("promptTokenCount") or 0)
        output_tokens = int(meta.get("candidatesTokenCount") or 0)
        if input_tokens == 0 and output_tokens == 0:
            return None
        # Gemini doesn't include model in response body; use empty string
        return {
            "model": data.get("modelVersion", ""),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    except Exception:
        return None
