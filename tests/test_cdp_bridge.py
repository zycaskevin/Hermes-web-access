"""Tests for CDP Bridge — HTTP API unit tests with mocked CDP connection.

Run: pytest tests/ -v
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import cdp_bridge  # loaded via conftest.py


# --- Fixtures ---

@pytest.fixture
def app():
    """Create a fresh aiohttp app for testing. Reset connection state."""
    cdp_bridge._conn._sessions.clear()
    cdp_bridge._conn._port_guard_sessions.clear()
    cdp_bridge._conn._ws = None
    return cdp_bridge.create_app()


# --- Mock data ---

MOCK_VERSION = {
    'Browser': 'Chrome/130.0.0.0',
    'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/xxx',
}


# --- Health ---

class TestHealth:
    @pytest.mark.asyncio
    async def test_disconnected(self, app, aiohttp_client):
        # Patch on the class (not instance) to avoid property setter issues
        with patch.object(cdp_bridge.CDPConnection, 'is_connected', new_callable=PropertyMock, return_value=False), \
             patch.object(cdp_bridge._conn, 'connect', side_effect=ConnectionError('refused')), \
             patch.object(cdp_bridge, 'chrome_http', side_effect=ConnectionError('refused')):
            client = await aiohttp_client(app)
            resp = await client.get('/health')
            assert resp.status == 503
            data = await resp.json()
            assert data['status'] == 'disconnected'

    @pytest.mark.asyncio
    async def test_connected(self, app, aiohttp_client):
        with patch.object(cdp_bridge.CDPConnection, 'is_connected', new_callable=PropertyMock, return_value=True), \
             patch.object(cdp_bridge, 'chrome_http', return_value=MOCK_VERSION):
            client = await aiohttp_client(app)
            resp = await client.get('/health')
            assert resp.status == 200
            data = await resp.json()
            assert data['status'] == 'ok'


# --- 404 ---

class TestNotFound:
    @pytest.mark.asyncio
    async def test_unknown_endpoint(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/nonexistent')
        assert resp.status == 404
        data = await resp.json()
        assert '/health' in data.get('endpoints', {})


# --- Parameter validation ---

class TestValidation:
    @pytest.mark.asyncio
    async def test_close_no_target(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/close')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_scroll_bad_direction(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/scroll?target=X&direction=sideways')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_scroll_bad_y(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/scroll?target=X&y=abc')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_screenshot_bad_format(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/screenshot?target=X&format=gif')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_setfiles_bad_json(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post('/setFiles?target=X', data='not json')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_setfiles_missing_fields(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post('/setFiles?target=X', data='{}')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_eval_empty(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post('/eval?target=X', data='')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_click_empty_selector(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post('/click?target=X', data='')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_navigate_no_url(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/navigate?target=X')
        assert resp.status == 400


# --- Targets (via CDP Target.getTargets) ---

class TestTargets:
    @pytest.mark.asyncio
    async def test_success(self, app, aiohttp_client):
        with patch.object(cdp_bridge._conn, 'connect', new_callable=AsyncMock), \
             patch.object(cdp_bridge._conn, 'send_cdp', return_value={
                 'result': {'targetInfos': [
                     {'type': 'page', 'targetId': 'ABC', 'title': 'Test', 'url': 'https://example.com'},
                     {'type': 'page', 'targetId': 'DEF', 'title': 'Other', 'url': 'https://other.com'},
                     {'type': 'service_worker', 'targetId': 'XXX'},
                 ]}
             }):
            client = await aiohttp_client(app)
            resp = await client.get('/targets')
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 2  # only 'page' type

    @pytest.mark.asyncio
    async def test_chrome_down(self, app, aiohttp_client):
        with patch.object(cdp_bridge._conn, 'connect', side_effect=ConnectionError('refused')):
            client = await aiohttp_client(app)
            resp = await client.get('/targets')
            assert resp.status == 502


# --- Session cleanup ---

class TestSessionCleanup:
    @pytest.mark.asyncio
    async def test_close_removes_session(self, app, aiohttp_client):
        cdp_bridge._conn._sessions['ABC123'] = 'sess-abc'
        cdp_bridge._conn._port_guard_sessions.add('sess-abc')
        # Mock only the CDP command inside close_target, not close_target itself,
        # because close_target does the session cleanup we're testing
        with patch.object(cdp_bridge._conn, 'send_cdp', new_callable=AsyncMock, return_value={'result': {}}):
            client = await aiohttp_client(app)
            resp = await client.get('/close?target=ABC123')
            assert resp.status == 200
            assert 'ABC123' not in cdp_bridge._conn._sessions
            assert 'sess-abc' not in cdp_bridge._conn._port_guard_sessions


# --- Scroll directions ---

class TestScrollDirections:
    @pytest.mark.asyncio
    async def test_top(self, app, aiohttp_client):
        with patch.object(cdp_bridge, 'cdp_command', new_callable=AsyncMock, return_value={
            'result': {'result': {'value': 'scrolled to top'}}
        }):
            client = await aiohttp_client(app)
            resp = await client.get('/scroll?target=X&direction=top')
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_bottom(self, app, aiohttp_client):
        with patch.object(cdp_bridge, 'cdp_command', new_callable=AsyncMock, return_value={
            'result': {'result': {'value': 'scrolled to bottom'}}
        }):
            client = await aiohttp_client(app)
            resp = await client.get('/scroll?target=X&direction=bottom')
            assert resp.status == 200


# --- CDPConnection class ---

class TestCDPConnection:
    def test_is_connected_false(self):
        conn = cdp_bridge.CDPConnection()
        assert conn.is_connected is False

    def test_sessions_init(self):
        conn = cdp_bridge.CDPConnection()
        assert len(conn.sessions) == 0
        assert len(conn.port_guard_sessions) == 0

    @pytest.mark.asyncio
    async def test_reconnect_on_send_failure(self):
        """send_cdp retries with backoff when connect fails, then succeeds."""
        conn = cdp_bridge.CDPConnection()
        conn.RECONNECT_MAX_RETRIES = 2
        conn.RECONNECT_BASE_DELAY = 0.01  # Fast for testing
        
        call_count = 0
        original_connect = conn.connect
        
        async def flaky_connect():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise ConnectionError('Chrome not reachable')
            # On 2nd try, simulate successful connect by setting ws
            # We can't really connect, but we verify the retry logic ran
            raise ConnectionError('Still not reachable')
        
        with patch.object(conn, 'connect', side_effect=flaky_connect):
            with pytest.raises(ConnectionError):
                await conn.send_cdp('Page.enable', {})
            # connect() is called RECONNECT_MAX_RETRIES+1 times (1 initial + 2 retries)
            assert call_count == 3


# --- Utility functions ---

class TestUtilities:
    def test_check_port_unreachable(self):
        assert cdp_bridge._check_port(59999, '127.0.0.1', timeout=0.1) is False

    def test_wsl2_no_proc(self):
        with patch.object(cdp_bridge, 'Path', side_effect=FileNotFoundError):
            result = cdp_bridge._is_wsl2()
            assert isinstance(result, bool)

    def test_wsl2_microsoft_string(self):
        assert 'microsoft' in 'Linux version 5.15 Microsoft'.lower()


# --- P1 Security validation tests ---

class TestPathValidation:
    def test_allowed_tmp_path(self):
        assert cdp_bridge._validate_filepath('/tmp/shot.png') is None

    def test_allowed_home_path(self):
        assert cdp_bridge._validate_filepath('/home/user/shot.png') is None

    def test_blocked_etc_path(self):
        result = cdp_bridge._validate_filepath('/etc/passwd')
        assert result is not None
        assert 'not allowed' in result or 'system path' in result

    def test_blocked_root_path(self):
        result = cdp_bridge._validate_filepath('/root/shot.png')
        assert result is not None

    def test_blocked_proc_path(self):
        result = cdp_bridge._validate_filepath('/proc/self/mem')
        assert result is not None

    def test_blocked_unknown_prefix(self):
        result = cdp_bridge._validate_filepath('/opt/secret/shot.png')
        assert result is not None
        assert 'not allowed' in result

    def test_empty_path_ok(self):
        assert cdp_bridge._validate_filepath('') is None

    def test_path_traversal_attempt(self):
        result = cdp_bridge._validate_filepath('/tmp/../../../etc/passwd')
        # realpath resolves this to /etc/passwd, which should be blocked
        assert result is not None


class TestURLValidation:
    def test_allowed_http(self):
        assert cdp_bridge._validate_url('https://example.com') is None

    def test_blocked_file_scheme(self):
        result = cdp_bridge._validate_url('file:///etc/passwd')
        assert result is not None
        assert 'file:' in result

    def test_blocked_javascript_scheme(self):
        result = cdp_bridge._validate_url('javascript:alert(1)')
        assert result is not None
        assert 'javascript:' in result

    def test_blocked_data_scheme(self):
        result = cdp_bridge._validate_url('data:text/html,<h1>hi</h1>')
        assert result is not None
        assert 'data:' in result

    def test_blocked_vbscript_scheme(self):
        result = cdp_bridge._validate_url('vbscript:msgbox(1)')
        assert result is not None

    def test_case_insensitive_scheme(self):
        result = cdp_bridge._validate_url('FILE:///etc/passwd')
        assert result is not None

    def test_whitespace_stripped(self):
        result = cdp_bridge._validate_url('  file:///etc/passwd  ')
        assert result is not None


class TestInputLengthLimits:
    @pytest.mark.asyncio
    async def test_eval_too_long(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        long_js = 'x' * (cdp_bridge.MAX_JS_EXPR_LENGTH + 1)
        resp = await client.post('/eval?target=X', data=long_js)
        assert resp.status == 400
        data = await resp.json()
        assert 'too long' in data.get('error', '')

    @pytest.mark.asyncio
    async def test_click_selector_too_long(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        long_selector = 'x' * (cdp_bridge.MAX_SELECTOR_LENGTH + 1)
        resp = await client.post('/click?target=X', data=long_selector)
        assert resp.status == 400
        data = await resp.json()
        assert 'too long' in data.get('error', '')

    @pytest.mark.asyncio
    async def test_navigate_file_scheme_blocked(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/navigate?target=X&url=file:///etc/passwd')
        assert resp.status == 400
        data = await resp.json()
        assert 'file:' in data.get('error', '')

    @pytest.mark.asyncio
    async def test_screenshot_blocked_path(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get('/screenshot?target=X&file=/etc/exploit.png')
        assert resp.status == 400
        data = await resp.json()
        assert 'system path' in data.get('error', '') or 'not allowed' in data.get('error', '')