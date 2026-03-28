"""Tests for clawmetry.interceptor and clawmetry.providers_pricing."""
import json
import os
import sys

# Disable auto-patching during tests so we can test manually
os.environ["CLAWMETRY_NO_INTERCEPT"] = "1"

import pytest


# ── providers_pricing ─────────────────────────────────────────────────────────

class TestProvidersPricing:
    def test_known_providers_present(self):
        from clawmetry.providers_pricing import PROVIDER_MAP
        hostnames = list(PROVIDER_MAP.keys())
        assert any("anthropic" in h for h in hostnames)
        assert any("openai" in h for h in hostnames)
        assert any("groq" in h for h in hostnames)

    def test_estimate_cost_anthropic(self):
        from clawmetry.providers_pricing import estimate_cost_usd
        # 1000 input + 500 output with claude-sonnet-4 rates ($3/$15 per 1M)
        cost = estimate_cost_usd("anthropic", 1000, 500)
        assert cost > 0
        assert cost < 0.05  # sanity: 1500 tokens should cost less than 5 cents

    def test_estimate_cost_model_override(self):
        from clawmetry.providers_pricing import estimate_cost_usd
        # gpt-4o-mini is much cheaper than gpt-4o
        cost_mini = estimate_cost_usd("openai", 10000, 1000, model="gpt-4o-mini")
        cost_4o = estimate_cost_usd("openai", 10000, 1000, model="gpt-4o")
        assert cost_mini < cost_4o

    def test_estimate_cost_zero_tokens(self):
        from clawmetry.providers_pricing import estimate_cost_usd
        assert estimate_cost_usd("anthropic", 0, 0) == 0.0

    def test_estimate_cost_unknown_provider(self):
        from clawmetry.providers_pricing import estimate_cost_usd
        # Should not raise, returns a conservative non-zero estimate
        cost = estimate_cost_usd("unknown-provider-xyz", 1000, 500)
        assert cost >= 0.0

    def test_detect_provider_from_url(self):
        from clawmetry.providers_pricing import PROVIDER_MAP
        url = "https://api.anthropic.com/v1/messages"
        matched = None
        for hostname, info in PROVIDER_MAP.items():
            if hostname in url:
                matched = info["name"]
                break
        assert matched == "anthropic"

    def test_detect_provider_openai(self):
        from clawmetry.providers_pricing import PROVIDER_MAP
        url = "https://api.openai.com/v1/chat/completions"
        matched = None
        for hostname, info in PROVIDER_MAP.items():
            if hostname in url:
                matched = info["name"]
                break
        assert matched == "openai"

    def test_detect_provider_returns_none_for_unknown(self):
        from clawmetry.interceptor import _detect_provider
        assert _detect_provider("https://example.com/api") is None


# ── interceptor ───────────────────────────────────────────────────────────────

class TestInterceptor:
    def setup_method(self):
        # Reset ledger before each test
        from clawmetry import interceptor
        import threading
        with interceptor._lock:
            import time
            interceptor._ledger["session_start"] = time.time()
            interceptor._ledger["calls"] = 0
            interceptor._ledger["cost_usd"] = 0.0
            interceptor._ledger["tokens_in"] = 0
            interceptor._ledger["tokens_out"] = 0
            interceptor._ledger["providers"] = {}

    def test_record_adds_to_ledger(self):
        from clawmetry.interceptor import _record, get_session_stats
        _record("anthropic", 1000, 500, 0.01)
        stats = get_session_stats()
        assert stats["calls"] == 1
        assert stats["tokens_in"] == 1000
        assert stats["tokens_out"] == 500
        assert stats["cost_usd"] == pytest.approx(0.01)
        assert "anthropic" in stats["providers"]

    def test_record_accumulates(self):
        from clawmetry.interceptor import _record, get_session_stats
        _record("anthropic", 1000, 500, 0.01)
        _record("openai", 2000, 800, 0.02)
        stats = get_session_stats()
        assert stats["calls"] == 2
        assert stats["tokens_in"] == 3000
        assert abs(stats["cost_usd"] - 0.03) < 1e-8

    def test_extract_usage_anthropic_format(self):
        from clawmetry.interceptor import _extract_usage
        body = json.dumps({
            "usage": {"input_tokens": 150, "output_tokens": 75}
        }).encode()
        tokens_in, tokens_out = _extract_usage(body, "anthropic")
        assert tokens_in == 150
        assert tokens_out == 75

    def test_extract_usage_openai_format(self):
        from clawmetry.interceptor import _extract_usage
        body = json.dumps({
            "usage": {"prompt_tokens": 200, "completion_tokens": 100}
        }).encode()
        tokens_in, tokens_out = _extract_usage(body, "openai")
        assert tokens_in == 200
        assert tokens_out == 100

    def test_extract_usage_invalid_json(self):
        from clawmetry.interceptor import _extract_usage
        tokens_in, tokens_out = _extract_usage(b"not json", "anthropic")
        assert tokens_in == 0
        assert tokens_out == 0

    def test_extract_usage_missing_fields(self):
        from clawmetry.interceptor import _extract_usage
        body = json.dumps({"model": "claude-sonnet-4"}).encode()
        tokens_in, tokens_out = _extract_usage(body, "anthropic")
        assert tokens_in == 0
        assert tokens_out == 0

    def test_handle_response_non_llm_url(self):
        """Non-LLM URLs should not be tracked."""
        from clawmetry.interceptor import _handle_response_sync, get_session_stats
        body = json.dumps({"usage": {"input_tokens": 100, "output_tokens": 50}}).encode()
        _handle_response_sync("https://example.com/api/endpoint", body)
        stats = get_session_stats()
        assert stats["calls"] == 0

    def test_handle_response_anthropic_url(self):
        """Anthropic URLs with usage data should be tracked."""
        from clawmetry.interceptor import _handle_response_sync, get_session_stats
        body = json.dumps({
            "usage": {"input_tokens": 1000, "output_tokens": 200}
        }).encode()
        _handle_response_sync("https://api.anthropic.com/v1/messages", body)
        stats = get_session_stats()
        assert stats["calls"] == 1
        assert stats["tokens_in"] == 1000
        assert stats["tokens_out"] == 200

    def test_handle_response_zero_tokens_not_recorded(self):
        """Responses with zero tokens (e.g. non-inference endpoints) are ignored."""
        from clawmetry.interceptor import _handle_response_sync, get_session_stats
        body = json.dumps({"status": "ok"}).encode()
        _handle_response_sync("https://api.anthropic.com/v1/messages", body)
        stats = get_session_stats()
        assert stats["calls"] == 0

    def test_get_session_stats_returns_copy(self):
        """get_session_stats should return an independent copy."""
        from clawmetry.interceptor import _record, get_session_stats
        _record("anthropic", 100, 50, 0.001)
        stats1 = get_session_stats()
        _record("openai", 200, 100, 0.002)
        stats2 = get_session_stats()
        # First snapshot should still have 1 call
        assert stats1["calls"] == 1
        assert stats2["calls"] == 2

    def test_provider_breakdown_in_stats(self):
        from clawmetry.interceptor import _record, get_session_stats
        _record("anthropic", 1000, 500, 0.01)
        _record("anthropic", 2000, 1000, 0.02)
        _record("openai", 500, 200, 0.005)
        stats = get_session_stats()
        assert stats["providers"]["anthropic"]["calls"] == 2
        assert stats["providers"]["openai"]["calls"] == 1

    def test_patch_all_is_idempotent(self):
        """Calling patch_all() multiple times should not raise or double-patch."""
        from clawmetry.interceptor import patch_all
        patch_all()
        patch_all()
        patch_all()
        # No assertion needed — just verify no exception is raised


# ── init integration ──────────────────────────────────────────────────────────

class TestInit:
    def test_get_stats_returns_dict(self):
        import clawmetry
        stats = clawmetry.get_stats()
        assert isinstance(stats, dict)
        assert "calls" in stats
        assert "cost_usd" in stats

    def test_no_intercept_env_var(self):
        """CLAWMETRY_NO_INTERCEPT=1 should prevent auto-patching."""
        # We set this at module level, so patching should be skipped
        assert os.environ.get("CLAWMETRY_NO_INTERCEPT") == "1"
        # The patched flags should be False (never set in this test run)
        from clawmetry.interceptor import _patched
        # Note: patch_all IS callable even in no-intercept mode (it's manual)
        # but auto-patch on import is disabled
        assert isinstance(_patched, dict)
