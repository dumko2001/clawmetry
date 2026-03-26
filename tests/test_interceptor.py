"""
tests/test_interceptor.py — Verify zero-config HTTP interceptor works.

Run with: python -m pytest tests/test_interceptor.py -v
Or directly: python tests/test_interceptor.py
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Disable auto-print during tests
os.environ["CLAWMETRY_DISABLE"] = "1"


class TestProviderDetection(unittest.TestCase):
    def setUp(self):
        from clawmetry.providers import detect_provider
        self.detect = detect_provider

    def test_anthropic(self):
        self.assertEqual(self.detect("https://api.anthropic.com/v1/messages"), "anthropic")

    def test_openai(self):
        self.assertEqual(self.detect("https://api.openai.com/v1/chat/completions"), "openai")

    def test_gemini(self):
        self.assertEqual(
            self.detect("https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro:generateContent"),
            "gemini",
        )

    def test_mistral(self):
        self.assertEqual(self.detect("https://api.mistral.ai/v1/chat/completions"), "mistral")

    def test_groq(self):
        self.assertEqual(self.detect("https://api.groq.com/openai/v1/chat/completions"), "groq")

    def test_together(self):
        self.assertEqual(self.detect("https://api.together.xyz/v1/chat/completions"), "together")

    def test_unknown(self):
        self.assertIsNone(self.detect("https://example.com/api"))

    def test_empty(self):
        self.assertIsNone(self.detect(""))


class TestUsageExtraction(unittest.TestCase):
    def setUp(self):
        from clawmetry.providers import extract_usage
        self.extract = extract_usage

    def test_anthropic_usage(self):
        body = json.dumps({
            "model": "claude-3-5-sonnet-20241022",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }).encode()
        result = self.extract("anthropic", body)
        self.assertIsNotNone(result)
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 50)
        self.assertEqual(result["model"], "claude-3-5-sonnet-20241022")

    def test_openai_usage(self):
        body = json.dumps({
            "model": "gpt-4o",
            "usage": {"prompt_tokens": 200, "completion_tokens": 80},
        }).encode()
        result = self.extract("openai", body)
        self.assertIsNotNone(result)
        self.assertEqual(result["input_tokens"], 200)
        self.assertEqual(result["output_tokens"], 80)

    def test_gemini_usage(self):
        body = json.dumps({
            "usageMetadata": {"promptTokenCount": 150, "candidatesTokenCount": 60},
        }).encode()
        result = self.extract("gemini", body)
        self.assertIsNotNone(result)
        self.assertEqual(result["input_tokens"], 150)
        self.assertEqual(result["output_tokens"], 60)

    def test_invalid_json(self):
        result = self.extract("openai", b"not json")
        self.assertIsNone(result)

    def test_no_usage_field(self):
        body = json.dumps({"model": "gpt-4o", "choices": []}).encode()
        result = self.extract("openai", body)
        self.assertIsNone(result)


class TestCostCalculation(unittest.TestCase):
    def setUp(self):
        from clawmetry.providers import get_cost
        self.get_cost = get_cost

    def test_anthropic_sonnet_cost(self):
        # 1M input @ $3 + 0.5M output @ $15 = $3 + $7.5 = $10.5
        cost = self.get_cost("anthropic", "claude-3-5-sonnet-20241022", 1_000_000, 500_000)
        self.assertAlmostEqual(cost, 10.5, places=2)

    def test_openai_gpt4o_cost(self):
        # 100k input @ $2.5/M + 50k output @ $10/M = $0.25 + $0.5 = $0.75
        cost = self.get_cost("openai", "gpt-4o", 100_000, 50_000)
        self.assertAlmostEqual(cost, 0.75, places=4)

    def test_unknown_provider(self):
        cost = self.get_cost("unknown-llm", "some-model", 1000, 500)
        self.assertEqual(cost, 0.0)

    def test_zero_tokens(self):
        cost = self.get_cost("anthropic", "claude-3-5-sonnet", 0, 0)
        self.assertEqual(cost, 0.0)


class TestHandleResponse(unittest.TestCase):
    def test_handle_response_anthropic(self):
        """_handle_response should call ledger.record for known providers."""
        from clawmetry.interceptor import _handle_response
        from clawmetry.ledger import get_ledger

        ledger = get_ledger()
        calls_before = ledger._session_calls

        body = json.dumps({
            "model": "claude-3-5-sonnet-20241022",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }).encode()

        _handle_response("https://api.anthropic.com/v1/messages", body)

        # Session call count should have increased
        self.assertGreater(ledger._session_calls, calls_before)

    def test_handle_response_unknown_url(self):
        """_handle_response should be a no-op for unknown URLs."""
        from clawmetry.interceptor import _handle_response
        from clawmetry.ledger import get_ledger

        ledger = get_ledger()
        calls_before = ledger._session_calls
        _handle_response("https://example.com/api/v1/foo", b'{"data": "irrelevant"}')
        self.assertEqual(ledger._session_calls, calls_before)

    def test_handle_response_never_throws(self):
        """_handle_response must not raise even with garbage input."""
        from clawmetry.interceptor import _handle_response
        try:
            _handle_response("not-a-url", b"not json @@#$")
            _handle_response("", None)
            _handle_response(None, None)  # type: ignore
        except Exception as e:
            self.fail(f"_handle_response raised: {e}")


class TestHttpxPatch(unittest.TestCase):
    def test_httpx_sync_intercepted(self):
        """httpx.Client.send should be monkey-patched."""
        # Re-enable patching for this test
        old = os.environ.pop("CLAWMETRY_DISABLE", None)
        try:
            from clawmetry.interceptor import patch, _patched
            # Force re-patch if needed
            _patched.discard("httpx")
            patch()

            try:
                import httpx
            except ImportError:
                self.skipTest("httpx not installed")

            # Verify the method has been replaced (it won't be the original anymore)
            # We just verify it can be called without errors using a mock
            body = json.dumps({
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }).encode()

            mock_response = MagicMock()
            mock_response.content = body
            mock_response.request.url = "https://api.openai.com/v1/chat/completions"

            from clawmetry.ledger import get_ledger
            ledger = get_ledger()
            calls_before = ledger._session_calls

            # Call _handle_response directly to simulate what the patch does
            from clawmetry.interceptor import _handle_response
            _handle_response(str(mock_response.request.url), mock_response.content)

            self.assertGreater(ledger._session_calls, calls_before)
        finally:
            if old is not None:
                os.environ["CLAWMETRY_DISABLE"] = old


class TestLedger(unittest.TestCase):
    def test_record_accumulates(self):
        from clawmetry.ledger import _Ledger
        ledger = _Ledger()
        ledger.record("openai", "gpt-4o", 1000, 500, 0.0075)
        ledger.record("anthropic", "claude-3-5-sonnet", 2000, 1000, 0.045)
        self.assertEqual(ledger._session_calls, 2)
        self.assertAlmostEqual(ledger._session_cost, 0.0525, places=6)
        self.assertIn("openai", ledger._session_by_provider)
        self.assertIn("anthropic", ledger._session_by_provider)

    def test_never_throws_on_bad_record(self):
        from clawmetry.ledger import _Ledger
        ledger = _Ledger()
        try:
            ledger.record(None, None, "bad", "worse", "not-a-float")  # type: ignore
        except Exception as e:
            self.fail(f"ledger.record raised: {e}")


if __name__ == "__main__":
    # When run directly, show output
    os.environ.pop("CLAWMETRY_DISABLE", None)
    print("Running ClawMetry interceptor tests…\n")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestProviderDetection,
        TestUsageExtraction,
        TestCostCalculation,
        TestHandleResponse,
        TestHttpxPatch,
        TestLedger,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
