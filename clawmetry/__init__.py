"""
clawmetry — Zero-config LLM cost tracking.

``import clawmetry`` is all you need. HTTP calls to known LLM providers are
automatically intercepted; token counts and costs are printed to the terminal
and (optionally) synced to the cloud dashboard when CLAWMETRY_API_KEY is set.

Design principles:
  - Never reads request bodies
  - Sub-millisecond overhead
  - Never throws (all errors silently swallowed)
  - Works with httpx (sync + async), requests, urllib out of the box
"""
from __future__ import annotations

import os as _os

__version__ = "0.1.0"

# ── Auto-patch on import (unless explicitly disabled) ──────────────────────────
# Set CLAWMETRY_DISABLE=1 to skip patching (useful in tests or CI where you
# don't want the output).

if _os.environ.get("CLAWMETRY_DISABLE", "").strip() not in ("1", "true", "yes"):
    try:
        from clawmetry.interceptor import patch as _patch
        _patch()
    except Exception:
        pass

# ── Public API ─────────────────────────────────────────────────────────────────

def get_ledger():
    """Return the active :class:`clawmetry.ledger._Ledger` singleton."""
    from clawmetry.ledger import get_ledger as _get
    return _get()


def reset():
    """Reset session counters (useful in long-running processes / tests)."""
    try:
        import time as _t
        ledger = get_ledger()
        import threading
        with ledger._lock:
            ledger._session_start = _t.monotonic()
            ledger._session_calls = 0
            ledger._session_cost = 0.0
            ledger._session_by_provider.clear()
    except Exception:
        pass


__all__ = [
    "__version__",
    "get_ledger",
    "reset",
]
