"""Integration tests -- hit real external APIs.
These require network access and RESEND_API_KEY env var.
Skipped automatically if SKIP_INTEGRATION=1 or no network.
"""
import os
import pytest
import requests

RESEND_KEY = os.environ.get("RESEND_API_KEY", "re_jWLL59fj_PBctxiwxDLFiWjBZ9MiJ4ems")
SKIP = os.environ.get("SKIP_INTEGRATION", "0") == "1"
LIVE_URL = os.environ.get("LIVE_URL", "https://clawmetry.com")

skip_if_no_integration = pytest.mark.skipif(SKIP, reason="SKIP_INTEGRATION=1")


# ── Resend API ───────────────────────────────────────────────────────────────

class TestResendIntegration:
    @skip_if_no_integration
    def test_api_key_valid(self):
        r = requests.get("https://api.resend.com/domains",
                         headers={"Authorization": f"Bearer {RESEND_KEY}"}, timeout=10)
        assert r.status_code == 200, f"Resend API key invalid or expired: {r.text}"

    @skip_if_no_integration
    def test_sender_domain_verified(self):
        """The domain used in FROM_EMAIL must be verified on Resend."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        r = requests.get("https://api.resend.com/domains",
                         headers={"Authorization": f"Bearer {RESEND_KEY}"}, timeout=10)
        domains = r.json().get("data", [])

        # Read actual FROM_EMAIL from app config
        from unittest.mock import MagicMock, patch
        with patch.dict("sys.modules", {
            "google.cloud.firestore": MagicMock(),
            "google.cloud": MagicMock(),
        }):
            import importlib
            import app as a
            from_email = a.FROM_EMAIL  # e.g. "ClawMetry <hello@aivira.co>"

        # Extract domain
        import re
        match = re.search(r"@([\w.-]+)>?", from_email)
        assert match, f"Could not parse domain from FROM_EMAIL: {from_email}"
        sender_domain = match.group(1)

        verified = [d for d in domains
                    if d["name"] == sender_domain and d["status"] == "verified"]
        assert verified, (
            f"FROM_EMAIL domain '{sender_domain}' is NOT verified on Resend!\n"
            f"Status: {[d for d in domains if d['name'] == sender_domain]}\n"
            "Fix: either verify the domain DNS or update FROM_EMAIL to a verified domain."
        )

    @skip_if_no_integration
    def test_can_send_email(self):
        """Send to Resend's test sink to verify end-to-end delivery."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from unittest.mock import MagicMock, patch
        with patch.dict("sys.modules", {
            "google.cloud.firestore": MagicMock(),
            "google.cloud": MagicMock(),
        }):
            import app as a
            from_email = a.FROM_EMAIL

        r = requests.post("https://api.resend.com/emails",
                          headers={"Authorization": f"Bearer {RESEND_KEY}",
                                   "Content-Type": "application/json"},
                          json={
                              "from": from_email,
                              "to": ["delivered@resend.dev"],  # Resend's test sink
                              "subject": "[CI] ClawMetry email delivery test",
                              "html": "<p>Automated CI test — ignore.</p>"
                          }, timeout=10)
        assert r.status_code in (200, 201), (
            f"Email send FAILED: {r.json()}\n"
            "Check FROM_EMAIL domain is verified on Resend."
        )


# ── Live site smoke tests ────────────────────────────────────────────────────

class TestLiveSite:
    @skip_if_no_integration
    def test_homepage_up(self):
        r = requests.get(LIVE_URL, timeout=15)
        assert r.status_code == 200

    @skip_if_no_integration
    def test_showcase_up(self):
        r = requests.get(f"{LIVE_URL}/showcase", timeout=15)
        assert r.status_code == 200

    @skip_if_no_integration
    def test_live_subscribe_api(self):
        """Hit the live subscribe endpoint with a test sentinel email."""
        r = requests.post(f"{LIVE_URL}/api/subscribe",
                          json={"email": "ci-test@resend.dev", "source": "ci"},
                          headers={"Content-Type": "application/json"},
                          timeout=15)
        # Should succeed (200) or rate-limit (429) -- never 500
        assert r.status_code in (200, 429), (
            f"Live subscribe returned {r.status_code}: {r.text}"
        )

    @skip_if_no_integration
    def test_live_copy_track_api(self):
        r = requests.post(f"{LIVE_URL}/api/copy-track",
                          json={"tab": "linux", "command": "pip install clawmetry", "source": "ci"},
                          headers={"Content-Type": "application/json"},
                          timeout=15)
        assert r.status_code == 200, f"copy-track returned {r.status_code}: {r.text}"

    @skip_if_no_integration
    def test_live_view_all_link(self):
        r = requests.get(LIVE_URL, timeout=15)
        assert 'href="/showcase"' in r.text, "Live site missing 'View all' link"

    @skip_if_no_integration
    def test_live_ph_links_format(self):
        r = requests.get(f"{LIVE_URL}/showcase", timeout=15)
        assert "#comment-43111" not in r.text, "Old broken PH comment IDs on live site"
        assert "?comment=" in r.text, "Live showcase missing ?comment= format PH links"
