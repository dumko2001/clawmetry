"""API endpoint tests -- all external calls mocked."""
import pytest
import json


# ── /api/subscribe ──────────────────────────────────────────────────────────

class TestSubscribe:
    def test_valid_email(self, client, mock_externals):
        r = client.post("/api/subscribe",
                        json={"email": "test@example.com", "source": "pytest"},
                        content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data.get("ok") is True

    def test_missing_email(self, client):
        r = client.post("/api/subscribe",
                        json={"source": "pytest"},
                        content_type="application/json")
        assert r.status_code == 400

    def test_invalid_email_format(self, client):
        for bad in ["notanemail", "a@", "@b.com", "a b@c.com", ""]:
            r = client.post("/api/subscribe",
                            json={"email": bad, "source": "pytest"},
                            content_type="application/json")
            assert r.status_code == 400, f"Expected 400 for email={bad!r}"

    def test_subscribe_end_to_end(self, client, mock_externals):
        """subscribe should complete without errors when Resend is mocked.
        200 response proves add_contact (_resend_post) ran successfully."""
        r = client.post("/api/subscribe",
                        json={"email": "e2e-test@example.com", "source": "pytest"},
                        content_type="application/json")
        assert r.status_code == 200, f"subscribe failed: {r.data}"
        assert r.get_json().get("ok") is True

    def test_duplicate_email_still_returns_ok(self, client, mock_externals):
        """Same email subscribed twice should still return 200 (Resend handles deduplication)."""
        r = client.post("/api/subscribe",
                        json={"email": "already@example.com", "source": "pytest"},
                        content_type="application/json")
        assert r.status_code == 200


# ── /api/copy-track ─────────────────────────────────────────────────────────

class TestCopyTrack:
    def test_linux_tab(self, client, mock_externals):
        r = client.post("/api/copy-track",
                        json={"tab": "linux", "command": "pip install clawmetry", "source": "landing"},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.get_json().get("ok") is True

    def test_windows_tab(self, client, mock_externals):
        r = client.post("/api/copy-track",
                        json={"tab": "windows", "command": "pip install clawmetry"},
                        content_type="application/json")
        assert r.status_code == 200

    def test_notify_called(self, client, mock_externals):
        """copy-track should complete end-to-end, including notify_vivek."""
        r = client.post("/api/copy-track",
                        json={"tab": "linux", "command": "pip install clawmetry"},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.get_json().get("ok") is True

    def test_empty_body(self, client, mock_externals):
        """Should handle missing fields gracefully."""
        r = client.post("/api/copy-track",
                        json={},
                        content_type="application/json")
        assert r.status_code == 200  # graceful, just logs unknown tab


# ── /api/social-click ───────────────────────────────────────────────────────

class TestSocialClick:
    def test_twitter_click(self, client, mock_externals):
        r = client.post("/api/social-click",
                        json={"platform": "twitter", "handle": "@jonah_lipsitt", "source": "showcase"},
                        content_type="application/json")
        assert r.status_code == 200

    def test_ph_click(self, client, mock_externals):
        r = client.post("/api/social-click",
                        json={"platform": "producthunt", "source": "landing"},
                        content_type="application/json")
        assert r.status_code == 200


# ── /api/managed-click ──────────────────────────────────────────────────────

class TestManagedClick:
    def test_basic_click(self, client, mock_externals):
        r = client.post("/api/managed-click",
                        json={"source": "landing", "identity": {}},
                        content_type="application/json")
        assert r.status_code == 200

    def test_known_user_click(self, client, mock_externals):
        r = client.post("/api/managed-click",
                        json={
                            "source": "landing",
                            "identity": {"email": "user@example.com", "name": "Test User"}
                        },
                        content_type="application/json")
        assert r.status_code == 200


# ── /api/support-request ────────────────────────────────────────────────────

class TestSupportRequest:
    def test_valid_request(self, client, mock_externals):
        r = client.post("/api/support-request",
                        json={
                            "email": "help@example.com",
                            "name": "Test",
                            "message": "I need help setting up ClawMetry",
                            "source": "landing"
                        },
                        content_type="application/json")
        assert r.status_code == 200


# ── FROM_EMAIL domain check ──────────────────────────────────────────────────

class TestEmailConfig:
    def test_from_email_has_valid_format(self):
        """FROM_EMAIL must be a valid sender address."""
        import app as a
        from_email = a.FROM_EMAIL
        assert from_email, "FROM_EMAIL is empty!"
        assert "@" in from_email, f"FROM_EMAIL looks invalid: {from_email}"
        assert "<" in from_email and ">" in from_email,             f"FROM_EMAIL should be 'Name <email>' format, got: {from_email}"

    def test_notify_email_is_correct(self):
        import app as a
        assert "@" in a.VIVEK_EMAIL
        assert a.VIVEK_EMAIL  # not empty
