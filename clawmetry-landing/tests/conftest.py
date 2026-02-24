"""Shared fixtures for ClawMetry landing tests."""
import pytest
import sys
import os
from unittest.mock import patch, MagicMock, Mock

# Add parent dir so app.py is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Firestore client mock (set up BEFORE importing app) ───────────────────
_fs_client_mock = MagicMock()
# Make .collection().add() return a proper (timestamp, ref) tuple
_ref_mock = MagicMock()
_ref_mock.id = "mock-doc-id"
_fs_client_mock.collection.return_value.add.return_value = (None, _ref_mock)
_fs_client_mock.collection.return_value.stream.return_value = []

_firestore_mod_mock = MagicMock()
_firestore_mod_mock.Client.return_value = _fs_client_mock

with patch.dict("sys.modules", {
    "google.cloud.firestore": _firestore_mod_mock,
    "google.cloud": MagicMock(),
    "google.auth": MagicMock(),
    "google.oauth2": MagicMock(),
}):
    import app as _app_module  # noqa: E402

VISITOR_STUB = {
    "ip": "1.2.3.4",
    "location": "Test City, NL",
    "user_agent": "pytest/1.0",
    "referer": "https://clawmetry.com/",
}


@pytest.fixture(scope="session")
def app():
    _app_module.app.config.update({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
    })
    return _app_module.app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def mock_externals():
    """Patch all external calls so no real HTTP/DB traffic leaves the test."""
    with patch("app._resend_post", return_value=(True, {"id": "mock-email-id"})) as mp, \
         patch("app._resend_get", return_value={"data": []}) as mg, \
         patch("app._get_visitor_info", return_value=VISITOR_STUB), \
         patch("app._fs", return_value=_fs_client_mock):
        yield {"resend_post": mp, "resend_get": mg}
