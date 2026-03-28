"""ClawMetry — OpenClaw Observability Dashboard.

Zero-config LLM cost tracking:

    import clawmetry  # that's it

Automatically patches httpx, requests, and urllib to intercept every LLM API
call, extract token counts and costs from the response, and print a summary
on session exit:

    clawmetry ▸ session: $0.23 (8 calls, 4m 12s) ── today: $1.47 ── ~$44/mo
               anthropic: $0.21 · openai: $0.02

Local mode (default): terminal output + local ledger at ~/.clawmetry/.
Cloud mode: set CLAWMETRY_API_KEY env var to sync to app.clawmetry.com.
"""
import re as _re
import os as _os

# Read version directly from dashboard.py without importing it (avoids circular import)
def _read_version():
    try:
        db = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), 'dashboard.py')
        with open(db, 'r', encoding='utf-8') as f:
            for line in f:
                m = _re.match(r'^__version__\s*=\s*["\'](.+?)["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"

__version__ = _read_version()


def main():
    """CLI entry point — delegates to dashboard.main()."""
    import sys, os
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from dashboard import main as _main
    _main()


# ── Auto-patch HTTP libraries on import ──────────────────────────────────────
# Only activate when imported directly (not when running as the dashboard CLI).
# Guard: skip if CLAWMETRY_NO_INTERCEPT=1 is set, or if we are the dashboard.
_intercept_disabled = (
    _os.environ.get("CLAWMETRY_NO_INTERCEPT", "").strip() in ("1", "true", "yes")
    or _os.environ.get("CLAWMETRY_DASHBOARD", "").strip() in ("1", "true", "yes")
)

if not _intercept_disabled:
    try:
        from clawmetry.interceptor import patch_all as _patch_all
        _patch_all()
    except Exception:
        pass  # never raise on import


def get_stats():
    """Return current session cost/token stats dict."""
    try:
        from clawmetry.interceptor import get_session_stats
        return get_session_stats()
    except Exception:
        return {}


__all__ = ["__version__", "main", "get_stats"]
