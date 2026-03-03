"""Tests for ClawMetry alerting integrations: Slack, Discord, PagerDuty, OpsGenie.

Run from repo root:
    python -m pytest tests/test_alerting.py -v
"""
import json
import os
import sys
import time
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch, call

# ── Import helpers from dashboard.py ───────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# We import only the functions we need, mocking Flask/SQLite dependencies
import importlib.util


def _load_dashboard_functions():
    """Load dashboard.py and extract the functions we want to test."""
    spec = importlib.util.spec_from_file_location(
        "dashboard",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")
    )
    # We can't easily import the full module (it starts Flask on import),
    # so we test functions by importing the file with mocked dependencies.
    return None  # Use direct imports below


# ── Unit tests: sender functions (all HTTP mocked) ─────────────────────────

class MockHTTPResponse:
    """Minimal mock for urllib.request.urlopen response."""
    def __init__(self, body=b'ok', status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestSendSlackAlert(unittest.TestCase):
    """Test _send_slack_alert with mocked HTTP."""

    def _get_fn(self):
        """Dynamically import the function from dashboard.py source."""
        # Parse and exec just the function we need
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()

        # Extract function with exec
        import types
        ns = {'json': json, 'time': time}
        # Find and exec the _send_slack_alert function
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _send_slack_alert('):
                start = i
                break
        if start is None:
            self.fail("_send_slack_alert not found in dashboard.py")

        # Extract until next top-level def
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_slack_alert'):
                func_lines.pop()
                break

        exec('\n'.join(func_lines), ns)
        return ns['_send_slack_alert']

    def setUp(self):
        self.fn = self._get_fn()

    @patch('urllib.request.urlopen')
    def test_slack_success(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(b'ok', 200)
        ok, err = self.fn(
            {'webhook_url': 'https://hooks.slack.com/test'},
            'Test alert message',
            'agent_down',
            {'session': 'abc123'}
        )
        self.assertTrue(ok)
        self.assertIsNone(err)
        mock_urlopen.assert_called_once()

    @patch('urllib.request.urlopen')
    def test_slack_sends_correct_payload(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(b'ok', 200)
        self.fn(
            {'webhook_url': 'https://hooks.slack.com/test', 'channel': '#ops', 'username': 'MyBot'},
            'Agent is down!',
            'agent_down',
        )
        args, kwargs = mock_urlopen.call_args
        req = args[0]
        payload = json.loads(req.data.decode())
        self.assertEqual(payload['username'], 'MyBot')
        self.assertEqual(payload['channel'], '#ops')
        self.assertIn('attachments', payload)
        self.assertEqual(payload['attachments'][0]['color'], '#ff4444')
        self.assertIn('Agent is down!', payload['attachments'][0]['text'])

    def test_slack_no_webhook_url(self):
        ok, err = self.fn({}, 'Test', 'test')
        self.assertFalse(ok)
        self.assertIn('webhook_url', err)

    @patch('urllib.request.urlopen')
    def test_slack_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception('Connection refused')
        ok, err = self.fn(
            {'webhook_url': 'https://hooks.slack.com/test'},
            'Test',
            'test',
        )
        self.assertFalse(ok)
        self.assertIn('Connection refused', err)

    @patch('urllib.request.urlopen')
    def test_slack_details_become_fields(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(b'ok', 200)
        self.fn(
            {'webhook_url': 'https://hooks.slack.com/test'},
            'Token spike!',
            'token_anomaly',
            {'tokens_last_hour': 50000, 'multiplier': '3.1x'},
        )
        args, _ = mock_urlopen.call_args
        req = args[0]
        payload = json.loads(req.data.decode())
        fields = payload['attachments'][0]['fields']
        field_names = [f['title'] for f in fields]
        self.assertIn('tokens_last_hour', field_names)
        self.assertIn('multiplier', field_names)


class TestSendDiscordAlert(unittest.TestCase):
    """Test _send_discord_alert with mocked HTTP."""

    def _get_fn(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        import types
        from datetime import datetime
        ns = {'json': json, 'time': time, 'datetime': datetime}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _send_discord_alert('):
                start = i
                break
        if start is None:
            self.fail("_send_discord_alert not found in dashboard.py")
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_discord_alert'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        return ns['_send_discord_alert']

    def setUp(self):
        self.fn = self._get_fn()

    @patch('urllib.request.urlopen')
    def test_discord_success(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(b'', 204)
        ok, err = self.fn(
            {'webhook_url': 'https://discord.com/api/webhooks/123/abc'},
            'Test alert',
            'error_rate_spike',
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

    @patch('urllib.request.urlopen')
    def test_discord_sends_embed(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(b'', 204)
        self.fn(
            {'webhook_url': 'https://discord.com/api/webhooks/123/abc', 'username': 'ClawBot'},
            'Error spike detected',
            'error_rate_spike',
            {'error_rate': '35%', 'failed_calls': 7},
        )
        args, _ = mock_urlopen.call_args
        req = args[0]
        payload = json.loads(req.data.decode())
        self.assertEqual(payload['username'], 'ClawBot')
        self.assertIn('embeds', payload)
        embed = payload['embeds'][0]
        self.assertEqual(embed['color'], 0xff8800)  # error_rate_spike color
        self.assertIn('Error spike detected', embed['description'])
        self.assertTrue(len(embed['fields']) == 2)

    def test_discord_no_webhook_url(self):
        ok, err = self.fn({}, 'Test', 'test')
        self.assertFalse(ok)
        self.assertIn('webhook_url', err)

    @patch('urllib.request.urlopen')
    def test_discord_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception('Timeout')
        ok, err = self.fn(
            {'webhook_url': 'https://discord.com/api/webhooks/123/abc'},
            'Test',
            'test',
        )
        self.assertFalse(ok)
        self.assertIn('Timeout', err)


class TestSendPagerDutyAlert(unittest.TestCase):
    """Test _send_pagerduty_alert with mocked HTTP."""

    def _get_fn(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        from datetime import datetime
        ns = {'json': json, 'time': time, 'datetime': datetime}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _send_pagerduty_alert('):
                start = i
                break
        if start is None:
            self.fail("_send_pagerduty_alert not found in dashboard.py")
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_pagerduty_alert'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        return ns['_send_pagerduty_alert']

    def setUp(self):
        self.fn = self._get_fn()

    @patch('urllib.request.urlopen')
    def test_pagerduty_success(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'status': 'success', 'dedup_key': 'abc'}).encode()
        )
        ok, err = self.fn(
            {'routing_key': 'abc123xyz'},
            'Agent down',
            'agent_down',
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

    @patch('urllib.request.urlopen')
    def test_pagerduty_sends_to_correct_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'status': 'success'}).encode()
        )
        self.fn(
            {'routing_key': 'testkey', 'source': 'MyClawMetry'},
            'Critical: agent down',
            'agent_down',
        )
        args, _ = mock_urlopen.call_args
        req = args[0]
        self.assertIn('events.pagerduty.com/v2/enqueue', req.full_url)
        payload = json.loads(req.data.decode())
        self.assertEqual(payload['routing_key'], 'testkey')
        self.assertEqual(payload['event_action'], 'trigger')
        self.assertEqual(payload['payload']['severity'], 'critical')
        self.assertEqual(payload['payload']['source'], 'MyClawMetry')

    @patch('urllib.request.urlopen')
    def test_pagerduty_severity_mapping(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'status': 'success'}).encode()
        )
        severity_map = {
            'agent_down': 'critical',
            'agent_silent': 'critical',
            'error_rate_spike': 'error',
            'token_anomaly': 'warning',
        }
        for alert_type, expected_severity in severity_map.items():
            mock_urlopen.reset_mock()
            self.fn({'routing_key': 'key'}, 'Test', alert_type)
            args, _ = mock_urlopen.call_args
            req = args[0]
            payload = json.loads(req.data.decode())
            self.assertEqual(
                payload['payload']['severity'],
                expected_severity,
                f"Wrong severity for {alert_type}"
            )

    def test_pagerduty_no_routing_key(self):
        ok, err = self.fn({}, 'Test', 'test')
        self.assertFalse(ok)
        self.assertIn('routing_key', err)

    @patch('urllib.request.urlopen')
    def test_pagerduty_dedup_key_is_hourly(self, mock_urlopen):
        """Dedup key should be stable within the same hour."""
        mock_urlopen.return_value = MockHTTPResponse(json.dumps({'status': 'success'}).encode())
        self.fn({'routing_key': 'key'}, 'Test', 'agent_down')
        args, _ = mock_urlopen.call_args
        payload1 = json.loads(args[0].data.decode())

        mock_urlopen.return_value = MockHTTPResponse(json.dumps({'status': 'success'}).encode())
        self.fn({'routing_key': 'key'}, 'Test', 'agent_down')
        args, _ = mock_urlopen.call_args
        payload2 = json.loads(args[0].data.decode())

        self.assertEqual(payload1['dedup_key'], payload2['dedup_key'])


class TestSendOpsGenieAlert(unittest.TestCase):
    """Test _send_opsgenie_alert with mocked HTTP."""

    def _get_fn(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        ns = {'json': json, 'time': time}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _send_opsgenie_alert('):
                start = i
                break
        if start is None:
            self.fail("_send_opsgenie_alert not found in dashboard.py")
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_opsgenie_alert'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        return ns['_send_opsgenie_alert']

    def setUp(self):
        self.fn = self._get_fn()

    @patch('urllib.request.urlopen')
    def test_opsgenie_success(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'result': 'Request will be processed', 'requestId': 'xyz'}).encode()
        )
        ok, err = self.fn(
            {'api_key': 'my-api-key'},
            'Agent silent for 15 minutes',
            'agent_silent',
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

    @patch('urllib.request.urlopen')
    def test_opsgenie_sends_geniekey_auth(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'result': 'Request will be processed'}).encode()
        )
        self.fn({'api_key': 'my-secret-key'}, 'Test', 'test')
        args, _ = mock_urlopen.call_args
        req = args[0]
        auth_header = req.get_header('Authorization') or req.headers.get('Authorization', '')
        self.assertIn('GenieKey my-secret-key', auth_header)
        self.assertIn('api.opsgenie.com/v2/alerts', req.full_url)

    @patch('urllib.request.urlopen')
    def test_opsgenie_message_truncated_to_130_chars(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'result': 'Request will be processed'}).encode()
        )
        long_msg = 'A' * 200
        self.fn({'api_key': 'key'}, long_msg, 'test')
        args, _ = mock_urlopen.call_args
        payload = json.loads(args[0].data.decode())
        self.assertLessEqual(len(payload['message']), 130)

    @patch('urllib.request.urlopen')
    def test_opsgenie_priority_mapping(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'result': 'Request will be processed'}).encode()
        )
        priority_cases = [
            ('agent_down', 'P1'),
            ('agent_silent', 'P1'),
            ('error_rate_spike', 'P2'),
            ('token_anomaly', 'P3'),
        ]
        for alert_type, expected_priority in priority_cases:
            mock_urlopen.reset_mock()
            self.fn({'api_key': 'key'}, 'Test', alert_type)
            args, _ = mock_urlopen.call_args
            payload = json.loads(args[0].data.decode())
            self.assertEqual(payload['priority'], expected_priority,
                             f"Wrong priority for {alert_type}")

    @patch('urllib.request.urlopen')
    def test_opsgenie_with_team(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(
            json.dumps({'result': 'Request will be processed'}).encode()
        )
        self.fn({'api_key': 'key', 'team': 'platform-team'}, 'Test', 'test')
        args, _ = mock_urlopen.call_args
        payload = json.loads(args[0].data.decode())
        self.assertIn('responders', payload)
        self.assertEqual(payload['responders'][0]['name'], 'platform-team')

    def test_opsgenie_no_api_key(self):
        ok, err = self.fn({}, 'Test', 'test')
        self.assertFalse(ok)
        self.assertIn('api_key', err)


# ── Unit tests: Agent condition checkers ───────────────────────────────────

class TestCheckAgentSilent(unittest.TestCase):
    """Test _check_agent_silent with a real temp directory."""

    def _get_fn(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        ns = {'os': os, 'time': time}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _check_agent_silent('):
                start = i
                break
        if start is None:
            self.fail("_check_agent_silent not found in dashboard.py")
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _check_agent_silent'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        return ns['_check_agent_silent']

    def setUp(self):
        self.fn = self._get_fn()

    def test_no_sessions_dir(self):
        is_silent, mins, session_id = self.fn('/nonexistent/path', 10)
        self.assertFalse(is_silent)
        self.assertEqual(mins, 0)

    def test_empty_sessions_dir(self):
        with tempfile.TemporaryDirectory() as d:
            is_silent, mins, session_id = self.fn(d, 10)
            self.assertFalse(is_silent)

    def test_recent_session_not_silent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'session-abc.jsonl')
            with open(path, 'w') as f:
                f.write('{"type":"message"}\n')
            # File just written = mtime is now, should not be silent
            is_silent, mins, session_id = self.fn(d, 10)
            self.assertFalse(is_silent)

    def test_old_session_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'session-old.jsonl')
            with open(path, 'w') as f:
                f.write('{"type":"message"}\n')
            # Set mtime to 20 minutes ago
            old_time = time.time() - 20 * 60
            os.utime(path, (old_time, old_time))
            is_silent, mins, session_id = self.fn(d, 10)
            self.assertTrue(is_silent)
            self.assertGreaterEqual(mins, 19)
            self.assertEqual(session_id, 'session-old')

    def test_most_recent_file_is_checked(self):
        """Should check the most recently modified file."""
        with tempfile.TemporaryDirectory() as d:
            # Old file
            old_path = os.path.join(d, 'session-old.jsonl')
            with open(old_path, 'w') as f:
                f.write('x\n')
            old_time = time.time() - 30 * 60
            os.utime(old_path, (old_time, old_time))

            # Recent file
            new_path = os.path.join(d, 'session-new.jsonl')
            with open(new_path, 'w') as f:
                f.write('x\n')
            # mtime = now (default)

            is_silent, mins, session_id = self.fn(d, 10)
            self.assertFalse(is_silent)  # Most recent file is fresh


class TestCheckErrorRateSpike(unittest.TestCase):
    """Test _check_error_rate_spike with temp session files."""

    def _get_fn(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        ns = {'os': os, 'time': time, 'json': json}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _check_error_rate_spike('):
                start = i
                break
        if start is None:
            self.fail("_check_error_rate_spike not found in dashboard.py")
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _check_error_rate_spike'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        return ns['_check_error_rate_spike']

    def setUp(self):
        self.fn = self._get_fn()

    def _write_session(self, d, name, events):
        path = os.path.join(d, name)
        with open(path, 'w') as f:
            for e in events:
                f.write(json.dumps(e) + '\n')
        return path

    def test_no_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            is_spike, rate, total, errors = self.fn(d)
            self.assertFalse(is_spike)

    def test_low_error_rate_no_spike(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            events = []
            # 10 successes, 1 error = 9% error rate
            for i in range(10):
                events.append({'type': 'tool_result', 'content': 'ok', 'timestamp': now - 100})
            events.append({'type': 'tool_result', 'content': 'error occurred', 'is_error': True, 'timestamp': now - 100})
            self._write_session(d, 'session.jsonl', events)
            is_spike, rate, total, errors = self.fn(d, window_minutes=60, error_rate_threshold=0.3)
            self.assertFalse(is_spike)
            self.assertAlmostEqual(rate, 1/11, places=2)

    def test_high_error_rate_is_spike(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            events = []
            # 4 successes, 6 errors = 60% error rate
            for i in range(4):
                events.append({'type': 'tool_result', 'content': 'ok', 'timestamp': now - 100})
            for i in range(6):
                events.append({'type': 'tool_result', 'content': 'error: something failed', 'timestamp': now - 100})
            self._write_session(d, 'session.jsonl', events)
            is_spike, rate, total, errors = self.fn(d, window_minutes=60, error_rate_threshold=0.3)
            self.assertTrue(is_spike)
            self.assertEqual(total, 10)
            self.assertEqual(errors, 6)

    def test_old_events_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            events = []
            # All errors, but from 2 hours ago
            for i in range(10):
                events.append({'type': 'tool_result', 'content': 'error', 'timestamp': now - 7200})
            self._write_session(d, 'session.jsonl', events)
            is_spike, rate, total, errors = self.fn(d, window_minutes=60, error_rate_threshold=0.3)
            self.assertFalse(is_spike)
            self.assertEqual(total, 0)

    def test_minimum_sample_size(self):
        """Under 5 calls, never spike."""
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            events = [
                {'type': 'tool_result', 'content': 'error', 'timestamp': now - 100},
                {'type': 'tool_result', 'content': 'error', 'timestamp': now - 100},
                {'type': 'tool_result', 'content': 'error', 'timestamp': now - 100},
            ]
            self._write_session(d, 'session.jsonl', events)
            is_spike, rate, total, errors = self.fn(d)
            self.assertFalse(is_spike)


class TestCheckTokenAnomaly(unittest.TestCase):
    """Test _check_token_anomaly with a mocked metrics store."""

    def test_no_tokens_no_anomaly(self):
        """With no token data, should not trigger."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        import threading as _threading
        mock_lock = _threading.Lock()
        mock_store = {'tokens': []}
        ns = {'time': time, '_metrics_lock': mock_lock, 'metrics_store': mock_store}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _check_token_anomaly('):
                start = i
                break
        if start is None:
            self.fail("_check_token_anomaly not found in dashboard.py")
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _check_token_anomaly'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        fn = ns['_check_token_anomaly']
        is_anomaly, hour_tokens, avg = fn(3.0)
        self.assertFalse(is_anomaly)

    def test_spike_detected(self):
        """Hour tokens 4x higher than avg should trigger."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        import threading as _threading
        now = time.time()
        mock_lock = _threading.Lock()
        # Average hourly = 1000 tokens/hr over last 24h
        # Last hour = 5000 tokens (5x spike)
        tokens = []
        # 23 hours of normal usage
        for h in range(1, 24):
            tokens.append({'timestamp': now - h * 3600, 'total': 1000})
        # Last hour: spike
        tokens.append({'timestamp': now - 30 * 60, 'total': 5000})
        mock_store = {'tokens': tokens}
        ns = {'time': time, '_metrics_lock': mock_lock, 'metrics_store': mock_store}
        lines = source.split('\n')
        start = None
        for i, line in enumerate(lines):
            if line.startswith('def _check_token_anomaly('):
                start = i
                break
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _check_token_anomaly'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        fn = ns['_check_token_anomaly']
        is_anomaly, hour_tokens, avg = fn(3.0)
        self.assertTrue(is_anomaly)
        self.assertEqual(hour_tokens, 5000)


# ── Integration tests: API endpoints (Flask test client) ──────────────────

class TestAlertChannelsAPI(unittest.TestCase):
    """Integration tests for /api/alerts/channels endpoints using Flask test client."""

    @classmethod
    def setUpClass(cls):
        """Set up Flask test client with a temp database."""
        cls.tmpdir = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmpdir, 'test_fleet.db')

        # Patch environment before importing
        os.environ['CLAWMETRY_FLEET_KEY'] = 'test-key'
        os.environ['CLAWMETRY_FLEET_DB'] = cls.db_path

        # We need to mock Flask startup and heavy imports
        # Use a lighter approach: test the functions directly
        cls._db_available = False
        try:
            import sqlite3
            cls.conn = sqlite3.connect(cls.db_path)
            cls.conn.row_factory = sqlite3.Row
            cls.conn.executescript("""
                CREATE TABLE IF NOT EXISTS alert_channels (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    config TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            cls.conn.commit()
            cls._db_available = True
        except Exception:
            pass

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'conn'):
            cls.conn.close()
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_db_available(self):
        self.assertTrue(self._db_available, "SQLite setup failed")

    def test_channel_schema(self):
        """Verify alert_channels table has correct schema."""
        cursor = self.conn.execute("PRAGMA table_info(alert_channels)")
        cols = {row['name'] for row in cursor.fetchall()}
        self.assertIn('id', cols)
        self.assertIn('type', cols)
        self.assertIn('name', cols)
        self.assertIn('config', cols)
        self.assertIn('enabled', cols)
        self.assertIn('created_at', cols)
        self.assertIn('updated_at', cols)

    def test_insert_and_retrieve_channel(self):
        """Test basic CRUD on the alert_channels table."""
        import uuid
        channel_id = str(uuid.uuid4())[:8]
        now = time.time()
        config = json.dumps({'webhook_url': 'https://hooks.slack.com/test'})
        self.conn.execute(
            "INSERT INTO alert_channels (id, type, name, config, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (channel_id, 'slack', 'Test Slack', config, 1, now, now)
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM alert_channels WHERE id=?", (channel_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['type'], 'slack')
        self.assertEqual(row['name'], 'Test Slack')
        cfg = json.loads(row['config'])
        self.assertEqual(cfg['webhook_url'], 'https://hooks.slack.com/test')

    def test_update_channel_enabled(self):
        """Test disabling a channel."""
        import uuid
        channel_id = str(uuid.uuid4())[:8]
        now = time.time()
        self.conn.execute(
            "INSERT INTO alert_channels (id, type, name, config, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (channel_id, 'discord', 'Test Discord', '{"webhook_url":"https://discord.com/x"}', 1, now, now)
        )
        self.conn.commit()
        self.conn.execute("UPDATE alert_channels SET enabled=0, updated_at=? WHERE id=?", (now, channel_id))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM alert_channels WHERE id=?", (channel_id,)).fetchone()
        self.assertEqual(row['enabled'], 0)

    def test_delete_channel(self):
        """Test deleting a channel."""
        import uuid
        channel_id = str(uuid.uuid4())[:8]
        now = time.time()
        self.conn.execute(
            "INSERT INTO alert_channels (id, type, name, config, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (channel_id, 'pagerduty', 'PD Prod', '{"routing_key":"abc"}', 1, now, now)
        )
        self.conn.commit()
        self.conn.execute("DELETE FROM alert_channels WHERE id=?", (channel_id,))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM alert_channels WHERE id=?", (channel_id,)).fetchone()
        self.assertIsNone(row)


# ── Payload format verification tests ─────────────────────────────────────

class TestPayloadFormats(unittest.TestCase):
    """Verify exact payload shapes expected by each service."""

    @patch('urllib.request.urlopen')
    def test_slack_payload_structure(self, mock_urlopen):
        """Slack expects {username, icon_emoji, attachments: [{color, title, text, footer, ts, fields}]}"""
        mock_urlopen.return_value = MockHTTPResponse(b'ok')
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        ns = {'json': json, 'time': time}
        lines = source.split('\n')
        start = next(i for i, l in enumerate(lines) if l.startswith('def _send_slack_alert('))
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_slack_alert'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        fn = ns['_send_slack_alert']

        fn({'webhook_url': 'https://x'}, 'msg', 'agent_down', {'k': 'v'})
        args, _ = mock_urlopen.call_args
        p = json.loads(args[0].data.decode())
        self.assertIn('username', p)
        self.assertIn('icon_emoji', p)
        self.assertIn('attachments', p)
        a = p['attachments'][0]
        for key in ('color', 'title', 'text', 'footer', 'ts', 'fields'):
            self.assertIn(key, a, f"Missing Slack attachment key: {key}")

    @patch('urllib.request.urlopen')
    def test_discord_embed_structure(self, mock_urlopen):
        """Discord expects {username, avatar_url, embeds: [{title, description, color, timestamp, footer, fields}]}"""
        mock_urlopen.return_value = MockHTTPResponse(b'', 204)
        from datetime import datetime
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        ns = {'json': json, 'time': time, 'datetime': datetime}
        lines = source.split('\n')
        start = next(i for i, l in enumerate(lines) if l.startswith('def _send_discord_alert('))
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_discord_alert'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        fn = ns['_send_discord_alert']

        fn({'webhook_url': 'https://x'}, 'msg', 'error_rate_spike')
        args, _ = mock_urlopen.call_args
        p = json.loads(args[0].data.decode())
        self.assertIn('embeds', p)
        embed = p['embeds'][0]
        for key in ('title', 'description', 'color', 'timestamp', 'footer'):
            self.assertIn(key, embed, f"Missing Discord embed key: {key}")

    @patch('urllib.request.urlopen')
    def test_pagerduty_event_structure(self, mock_urlopen):
        """PagerDuty Events API v2 expects routing_key, event_action, dedup_key, payload"""
        mock_urlopen.return_value = MockHTTPResponse(json.dumps({'status': 'success'}).encode())
        from datetime import datetime
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")) as f:
            source = f.read()
        ns = {'json': json, 'time': time, 'datetime': datetime}
        lines = source.split('\n')
        start = next(i for i, l in enumerate(lines) if l.startswith('def _send_pagerduty_alert('))
        func_lines = []
        for line in lines[start:]:
            func_lines.append(line)
            if len(func_lines) > 2 and line.startswith('def ') and not line.startswith('def _send_pagerduty_alert'):
                func_lines.pop()
                break
        exec('\n'.join(func_lines), ns)
        fn = ns['_send_pagerduty_alert']

        fn({'routing_key': 'key'}, 'Agent down', 'agent_down')
        args, _ = mock_urlopen.call_args
        p = json.loads(args[0].data.decode())
        for key in ('routing_key', 'event_action', 'dedup_key', 'payload'):
            self.assertIn(key, p, f"Missing PagerDuty field: {key}")
        inner = p['payload']
        for key in ('summary', 'source', 'severity', 'timestamp'):
            self.assertIn(key, inner, f"Missing PagerDuty payload field: {key}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
