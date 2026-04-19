#!/usr/bin/env python3
"""CDP Bridge - Full Chrome DevTools Protocol proxy for Hermes Agent.
Bridges all CDP operations from WSL to Windows Chrome via TCP proxy.

Usage:
  # WSL2 (auto-detected):
  python3 cdp-bridge.py
  
  # Native Linux/macOS:
  CHROME_HOST=127.0.0.1 CHROME_PORT=9222 python3 cdp-bridge.py

Architecture:
  Agent → localhost:3456 (this bridge) → Chrome CDP via single persistent WebSocket
  Browser-level WS is maintained; commands routed via sessionId to specific targets.
  This matches upstream cdp-proxy.mjs architecture for efficiency and event handling.
"""
import asyncio
import aiohttp
from aiohttp import web
import json
import logging
import os
import signal
import socket
import sys
import base64
from pathlib import Path

# --- Logging ---
logger = logging.getLogger('cdp-bridge')
logging.basicConfig(
    level=logging.INFO,
    format='[%(name)s] %(levelname)s: %(message)s',
    stream=sys.stdout,
)

# --- Config ---
PROXY_PORT = int(os.environ.get('CDP_PROXY_PORT', '3456'))
BRIDGE_HOST = os.environ.get('CDP_BRIDGE_HOST', '127.0.0.1')  # Listen address (security: default localhost only)
CHROME_HTTP_TIMEOUT = int(os.environ.get('CHROME_HTTP_TIMEOUT', '10'))  # seconds
CDP_WS_TIMEOUT = int(os.environ.get('CDP_WS_TIMEOUT', '30'))  # seconds
NAV_POLL_INTERVAL = float(os.environ.get('NAV_POLL_INTERVAL', '0.5'))
NAV_POLL_TIMEOUT = int(os.environ.get('NAV_POLL_TIMEOUT', '15'))

# --- Platform detection ---
def _is_wsl2() -> bool:
    """Check if running under WSL2."""
    try:
        version_text = Path('/proc/version').read_text().lower()
        return 'microsoft' in version_text
    except (OSError, FileNotFoundError):
        return False

def _get_wsl2_gateway() -> str:
    """Get Windows gateway IP for WSL2."""
    import subprocess
    try:
        result = subprocess.run(
            ['ip', 'route', 'show', 'default'],
            capture_output=True, text=True, timeout=5,
        )
        parts = result.stdout.strip().split()
        return parts[-1] if parts else '127.0.0.1'
    except Exception as e:
        logger.warning(f'Failed to detect WSL2 gateway: {e}')
        return '127.0.0.1'

# --- Auto-detect Chrome CDP host ---
if os.environ.get('CHROME_HOST'):
    CHROME_HOST = os.environ['CHROME_HOST']
    CHROME_PORT = int(os.environ.get('CHROME_PORT', '9222'))
elif _is_wsl2():
    CHROME_HOST = _get_wsl2_gateway()
    CHROME_PORT = int(os.environ.get('CHROME_PORT', '9223'))
else:
    CHROME_HOST = os.environ.get('CHROME_HOST', '127.0.0.1')
    CHROME_PORT = int(os.environ.get('CHROME_PORT', '9222'))

# --- Chrome auto-discovery ---
def _discover_chrome_port() -> int | None:
    """Try to discover Chrome's debug port from DevToolsActivePort file."""
    home = Path.home()
    possible_paths: list[Path] = []
    
    if sys.platform == 'darwin':
        possible_paths = [
            home / 'Library/Application Support/Google/Chrome/DevToolsActivePort',
            home / 'Library/Application Support/Google/Chrome Canary/DevToolsActivePort',
            home / 'Library/Application Support/Chromium/DevToolsActivePort',
        ]
    elif sys.platform == 'linux':
        possible_paths = [
            home / '.config/google-chrome/DevToolsActivePort',
            home / '.config/chromium/DevToolsActivePort',
        ]
    elif sys.platform == 'win32':
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        if local_app_data:
            possible_paths = [
                Path(local_app_data) / 'Google/Chrome/User Data/DevToolsActivePort',
                Path(local_app_data) / 'Chromium/User Data/DevToolsActivePort',
            ]
    
    for p in possible_paths:
        try:
            lines = p.read_text().strip().split('\n')
            port = int(lines[0])
            if 0 < port < 65536 and _check_port(port):
                logger.info(f'Discovered Chrome debug port from {p}: {port}')
                return port
        except (OSError, ValueError, IndexError):
            continue
    
    for port in (9222, 9229, 9333):
        if _check_port(port):
            logger.info(f'Discovered Chrome debug port by probing: {port}')
            return port
    
    return None

def _check_port(port: int, host: str = '127.0.0.1', timeout: float = 2.0) -> bool:
    """Check if a TCP port is listening (TCP connect probe, avoids WS auth popup)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, socket.timeout):
        return False

def _check_port_available(port: int, host: str = '127.0.0.1') -> bool:
    """Check if a port is available for binding."""
    try:
        with socket.create_server((host, port)):
            return True
    except OSError:
        return False

# Auto-discover Chrome port if not explicitly set and not WSL2
if not os.environ.get('CHROME_HOST') and not _is_wsl2():
    discovered = _discover_chrome_port()
    if discovered:
        CHROME_PORT = discovered

# --- Shared HTTP session ---
_shared_session: aiohttp.ClientSession | None = None

async def get_session() -> aiohttp.ClientSession:
    """Get or create the shared aiohttp ClientSession."""
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession()
    return _shared_session

# --- Browser-level WebSocket manager (core architecture change) ---
# Maintains a single persistent WS connection to Chrome's browser endpoint.
# Commands are routed to specific targets via sessionId.
# CDP events (Target.attachedToTarget, Fetch.requestPaused) are processed here.

class CDPConnection:
    """Manages a persistent browser-level WebSocket connection to Chrome CDP.
    
    This replaces the old architecture where each cdp_command() created a new WS.
    Now we maintain one connection and route via sessionId, matching upstream.
    """
    
    def __init__(self):
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._cmd_id = 0
        self._pending: dict[int, asyncio.Future] = {}  # cmd_id -> Future
        self._sessions: dict[str, str] = {}  # targetId -> sessionId
        self._port_guard_sessions: set[str] = set()
        self._chrome_port: int | None = None
        self._chrome_ws_path: str | None = None  # from DevToolsActivePort
        self._connecting: asyncio.Event = asyncio.Event()
        self._connecting.set()  # not currently connecting
        self._closed = False
    
    @property
    def sessions(self) -> dict[str, str]:
        return self._sessions
    
    @property
    def port_guard_sessions(self) -> set[str]:
        return self._port_guard_sessions
    
    async def chrome_http(self, path: str, method: str = 'GET') -> dict | list:
        """Make HTTP request to Chrome CDP."""
        url = f'http://{CHROME_HOST}:{CHROME_PORT}{path}'
        session = await get_session()
        try:
            async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=CHROME_HTTP_TIMEOUT)) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    return {'_raw': text, '_status': resp.status}
        except aiohttp.ClientConnectorError as e:
            logger.error(f'Cannot connect to Chrome at {CHROME_HOST}:{CHROME_PORT}: {e}')
            raise ConnectionError(f'Chrome not reachable at {CHROME_HOST}:{CHROME_PORT}') from e
        except aiohttp.ClientTimeout:
            logger.error(f'Chrome HTTP request timed out: {path}')
            raise TimeoutError(f'Chrome HTTP request timed out: {path}') from None
        except aiohttp.ClientError as e:
            logger.error(f'Chrome HTTP request failed: {e}')
            raise
    
    async def connect(self) -> None:
        """Connect to Chrome's browser-level WebSocket. Reuses connection if alive."""
        if self._ws and not self._ws.closed:
            return
        
        # Prevent concurrent connection attempts
        if not self._connecting.is_set():
            await self._connecting.wait()
            # Someone else connected while we waited
            if self._ws and not self._ws.closed:
                return
        
        self._connecting.clear()
        try:
            # Discover Chrome WS URL
            ws_url = await self._discover_ws_url()
            if not ws_url:
                raise ConnectionError('Cannot find Chrome WebSocket URL')
            
            session = await get_session()
            self._ws = await session.ws_connect(ws_url)
            self._closed = False
            
            # Start event listener task
            asyncio.create_task(self._listen_events())
            
            logger.info(f'Connected to Chrome CDP at {ws_url}')
        except Exception:
            self._ws = None
            raise
        finally:
            self._connecting.set()
    
    async def _discover_ws_url(self) -> str | None:
        """Discover Chrome's browser WebSocket URL."""
        # Try DevToolsActivePort with wsPath (like upstream)
        home = Path.home()
        possible_paths: list[Path] = []
        
        if sys.platform == 'darwin':
            possible_paths = [home / 'Library/Application Support/Google/Chrome/DevToolsActivePort']
        elif sys.platform == 'linux':
            possible_paths = [home / '.config/google-chrome/DevToolsActivePort']
        elif sys.platform == 'win32':
            local_app_data = os.environ.get('LOCALAPPDATA', '')
            if local_app_data:
                possible_paths = [Path(local_app_data) / 'Google/Chrome/User Data/DevToolsActivePort']
        
        for p in possible_paths:
            try:
                lines = p.read_text().strip().split('\n')
                port = int(lines[0])
                if 0 < port < 65536 and _check_port(port):
                    ws_path = lines[1] if len(lines) > 1 else None
                    if ws_path:
                        self._chrome_port = port
                        self._chrome_ws_path = ws_path
                        return f'ws://127.0.0.1:{port}{ws_path}'
            except (OSError, ValueError, IndexError):
                continue
        
        # Fallback: use /json/version
        try:
            version = await self.chrome_http('/json/version')
            ws_url = version.get('webSocketDebuggerUrl', '')
            if ws_url:
                return ws_url
        except Exception:
            pass
        
        return None
    
    async def _listen_events(self) -> None:
        """Background task: listen for CDP events on the browser WS."""
        if not self._ws:
            return
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    
                    # Response to a command we sent
                    if 'id' in data and data['id'] in self._pending:
                        fut = self._pending.pop(data['id'], None)
                        if fut and not fut.done():
                            fut.set_result(data)
                        continue
                    
                    # CDP events
                    method = data.get('method', '')
                    
                    # Track sessions from Target.attachedToTarget events
                    if method == 'Target.attachedToTarget':
                        params = data.get('params', {})
                        session_id = params.get('sessionId')
                        target_info = params.get('targetInfo', {})
                        target_id = target_info.get('targetId')
                        if session_id and target_id:
                            self._sessions[target_id] = session_id
                            logger.debug(f'Event: session attached {target_id[:8]} → {session_id[:8]}')
                    
                    # Handle Fetch.requestPaused (port guard)
                    if method == 'Fetch.requestPaused':
                        request_id = data.get('params', {}).get('requestId')
                        sid = data.get('sessionId')
                        if request_id:
                            try:
                                await self.send_cdp('Fetch.failRequest',
                                    {'requestId': request_id, 'errorReason': 'ConnectionRefused'},
                                    session_id=sid)
                            except Exception:
                                pass
                    
                    # Handle target detached
                    if method == 'Target.detachedFromTarget':
                        params = data.get('params', {})
                        sid = params.get('sessionId')
                        target_id = params.get('targetId')
                        if target_id:
                            self._sessions.pop(target_id, None)
                            if sid:
                                self._port_guard_sessions.discard(sid)
                
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.warning(f'Browser WS closed/error: {msg.type}')
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f'Event listener error: {e}')
        finally:
            # Mark disconnected, reject all pending
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError('Browser WebSocket closed'))
            self._pending.clear()
            self._sessions.clear()
            self._port_guard_sessions.clear()
            self._ws = None
    
    async def send_cdp(self, method: str, params: dict | None = None,
                        session_id: str | None = None, timeout: int | None = None) -> dict:
        """Send a CDP command via the persistent browser WebSocket."""
        await self.connect()
        
        if not self._ws or self._ws.closed:
            raise ConnectionError('Not connected to Chrome CDP')
        
        self._cmd_id += 1
        current_id = self._cmd_id
        msg: dict = {'id': current_id, 'method': method}
        if params:
            msg['params'] = params
        if session_id:
            msg['sessionId'] = session_id
        
        # Create future for response
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[current_id] = fut
        
        timeout_s = timeout or CDP_WS_TIMEOUT
        try:
            await self._ws.send_str(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(current_id, None)
            raise TimeoutError(f'CDP command {method} timed out after {timeout_s}s')
        except Exception as e:
            self._pending.pop(current_id, None)
            raise
    
    async def ensure_session(self, target_id: str) -> str:
        """Get or create a CDP session for the target."""
        if target_id in self._sessions:
            return self._sessions[target_id]
        
        result = await self.send_cdp('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
        session_id = result.get('result', {}).get('sessionId')
        if not session_id:
            raise RuntimeError(f"Failed to attach to target {target_id}: {result}")
        
        self._sessions[target_id] = session_id
        await self._enable_port_guard(target_id, session_id)
        return session_id
    
    async def _enable_port_guard(self, target_id: str, session_id: str) -> None:
        """Intercept page requests to Chrome's debug port (anti-detection)."""
        if session_id in self._port_guard_sessions:
            return
        try:
            port = self._chrome_port or CHROME_PORT
            await self.send_cdp('Fetch.enable', {
                'patterns': [
                    {'urlPattern': f'http://127.0.0.1:{port}/*', 'requestStage': 'Request'},
                    {'urlPattern': f'http://localhost:{port}/*', 'requestStage': 'Request'},
                ]
            }, session_id=session_id)
            self._port_guard_sessions.add(session_id)
            logger.debug(f'Port guard enabled for session {session_id[:8]}...')
        except Exception as e:
            logger.warning(f'Port guard failed for session {session_id[:8]}... (anti-detection disabled): {e}')
    
    async def close_target(self, target_id: str) -> None:
        """Close a target and clean up its session."""
        session_id = self._sessions.pop(target_id, None)
        if session_id:
            self._port_guard_sessions.discard(session_id)
        try:
            await self.send_cdp('Target.closeTarget', {'targetId': target_id})
        except Exception:
            pass
    
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed


# Global CDP connection
_conn = CDPConnection()


# --- Helper functions (thin wrappers for backward compatibility) ---

async def chrome_http(path: str, method: str = 'GET') -> dict | list:
    """Make HTTP request to Chrome CDP."""
    return await _conn.chrome_http(path, method)


async def cdp_command(method: str, params: dict | None = None,
                       target_id: str | None = None, timeout: int | None = None) -> dict:
    """Send a CDP command. Uses sessionId if target_id resolves to a known session."""
    if target_id:
        session_id = await _conn.ensure_session(target_id)
        return await _conn.send_cdp(method, params, session_id=session_id, timeout=timeout)
    return await _conn.send_cdp(method, params, timeout=timeout)


async def wait_for_load(target_id: str, timeout_ms: int | None = None) -> str:
    """Poll readyState until 'complete' or timeout."""
    timeout_ms = timeout_ms or NAV_POLL_TIMEOUT * 1000
    try:
        await cdp_command('Page.enable', {}, target_id=target_id)
    except RuntimeError:
        pass
    
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        try:
            result = await cdp_command('Runtime.evaluate', {
                'expression': 'document.readyState',
                'returnByValue': True,
            }, target_id=target_id)
            if result.get('result', {}).get('result', {}).get('value') == 'complete':
                return 'complete'
        except RuntimeError:
            pass
        await asyncio.sleep(NAV_POLL_INTERVAL)
    logger.warning(f'waitForLoad timed out after {timeout_ms}ms for target {target_id[:8]}...')
    return 'timeout'


# --- HTTP API Handlers ---

async def handle_targets(request: web.Request) -> web.Response:
    """List Chrome targets: GET /targets"""
    try:
        # Use CDP command instead of raw HTTP for consistent session tracking
        await _conn.connect()
        result = await _conn.send_cdp('Target.getTargets')
        pages = [t for t in result.get('result', {}).get('targetInfos', []) if t.get('type') == 'page']
        return web.json_response(pages)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_version(request: web.Request) -> web.Response:
    """Chrome version: GET /json/version"""
    try:
        data = await chrome_http('/json/version')
        return web.json_response(data)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_new(request: web.Request) -> web.Response:
    """Open new tab: GET /new?url=..."""
    url = request.query.get('url', 'about:blank')
    try:
        await _conn.connect()
        result = await _conn.send_cdp('Target.createTarget', {'url': url, 'background': True})
        target_id = result.get('result', {}).get('targetId')
        if target_id and url != 'about:blank':
            try:
                await wait_for_load(target_id)
            except Exception:
                await asyncio.sleep(1)
        return web.json_response({'targetId': target_id})
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_close(request: web.Request) -> web.Response:
    """Close tab: GET /close?target=ID"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    try:
        await _conn.close_target(target_id)
        return web.json_response({'ok': True})
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_info(request: web.Request) -> web.Response:
    """Page info: GET /info?target=ID"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    try:
        result = await cdp_command('Runtime.evaluate', {
            'expression': 'JSON.stringify({title: document.title, url: location.href, ready: document.readyState})',
            'returnByValue': True,
            'awaitPromise': True,
        }, target_id=target_id)
        value = result.get('result', {}).get('result', {}).get('value', '{}')
        if isinstance(value, str):
            try:
                return web.json_response(json.loads(value))
            except json.JSONDecodeError:
                pass
        return web.json_response({'error': 'Failed to get page info'}, status=502)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_eval(request: web.Request) -> web.Response:
    """Execute JS: POST /eval?target=ID, body=expression"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    expression = await request.text()
    if not expression:
        return web.json_response({'error': 'Empty JS expression'}, status=400)
    try:
        result = await cdp_command('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
        }, target_id=target_id)
        inner = result.get('result', {})
        if inner.get('result', {}).get('value') is not None:
            return web.json_response({'value': inner['result']['value']})
        elif inner.get('exceptionDetails'):
            err_text = inner['exceptionDetails'].get('text', 'JS exception')
            return web.json_response({'error': err_text}, status=400)
        return web.json_response(result)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_navigate(request: web.Request) -> web.Response:
    """Navigate: GET /navigate?target=ID&url=URL"""
    target_id = request.query.get('target', '')
    url = request.query.get('url', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    if not url:
        return web.json_response({'error': 'Missing url parameter'}, status=400)
    try:
        result = await cdp_command('Page.navigate', {'url': url}, target_id=target_id)
        await wait_for_load(target_id)
        return web.json_response(result.get('result', result))
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_back(request: web.Request) -> web.Response:
    """Go back: GET /back?target=ID"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    try:
        await cdp_command('Runtime.evaluate', {'expression': 'history.back()'}, target_id=target_id)
        await wait_for_load(target_id)
        return web.json_response({'ok': True})
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_click(request: web.Request) -> web.Response:
    """Click element: POST /click?target=ID, body=selector"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    selector = await request.text()
    if not selector:
        return web.json_response({'error': 'POST body needs a CSS selector'}, status=400)
    try:
        selector_json = json.dumps(selector)
        js = f'''(() => {{
            const el = document.querySelector({selector_json});
            if (!el) return {{ error: "Element not found: " + {selector_json} }};
            el.scrollIntoView({{ block: 'center' }});
            el.click();
            return {{ clicked: true, tag: el.tagName, text: (el.textContent || '').slice(0, 100) }};
        }})()'''
        result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True, 'awaitPromise': True,
        }, target_id=target_id)
        value = result.get('result', {}).get('result', {}).get('value', {})
        if isinstance(value, dict) and value.get('error'):
            return web.json_response(value, status=400)
        return web.json_response(value if value else result)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_click_at(request: web.Request) -> web.Response:
    """Real mouse click: POST /clickAt?target=ID, body=selector"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    selector = await request.text()
    if not selector:
        return web.json_response({'error': 'POST body needs a CSS selector'}, status=400)
    try:
        selector_json = json.dumps(selector)
        js = f'''(() => {{
            const el = document.querySelector({selector_json});
            if (!el) return {{ error: "Element not found: " + {selector_json} }};
            el.scrollIntoView({{ block: 'center' }});
            const rect = el.getBoundingClientRect();
            return {{ x: rect.x + rect.width/2, y: rect.y + rect.height/2, tag: el.tagName, text: (el.textContent || '').slice(0, 100) }};
        }})()'''
        pos_result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True, 'awaitPromise': True,
        }, target_id=target_id)
        pos = pos_result.get('result', {}).get('result', {}).get('value', {})
        if isinstance(pos, dict) and pos.get('error'):
            return web.json_response(pos, status=400)
        x, y = pos.get('x', 0), pos.get('y', 0)
        for typ in ('mousePressed', 'mouseReleased'):
            await cdp_command('Input.dispatchMouseEvent', {
                'type': typ, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
            }, target_id=target_id)
        return web.json_response({'clicked': True, 'x': x, 'y': y, 'tag': pos.get('tag'), 'text': pos.get('text', '')[:100]})
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_scroll(request: web.Request) -> web.Response:
    """Scroll: GET /scroll?target=ID&y=N&direction=down|up|top|bottom"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    y_raw = request.query.get('y', '')
    direction = request.query.get('direction', 'down')
    if direction not in ('down', 'up', 'top', 'bottom'):
        return web.json_response({'error': f'Invalid direction: {direction}. Use down|up|top|bottom'}, status=400)
    try:
        if direction == 'top':
            js = 'window.scrollTo(0, 0); "scrolled to top"'
        elif direction == 'bottom':
            js = 'window.scrollTo(0, document.body.scrollHeight); "scrolled to bottom"'
        elif direction == 'up':
            try:
                y_val = abs(int(y_raw)) if y_raw else 500
            except ValueError:
                return web.json_response({'error': f'Invalid y value: {y_raw}'}, status=400)
            js = f'window.scrollBy(0, -{y_val}); "scrolled up {y_val}px"'
        else:
            try:
                y_val = abs(int(y_raw)) if y_raw else 500
            except ValueError:
                return web.json_response({'error': f'Invalid y value: {y_raw}'}, status=400)
            js = f'window.scrollBy(0, {y_val}); "scrolled down {y_val}px"'
        result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True,
        }, target_id=target_id)
        await asyncio.sleep(0.8)
        value = result.get('result', {}).get('result', {}).get('value')
        return web.json_response({'value': value})
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_screenshot(request: web.Request) -> web.Response:
    """Screenshot: GET /screenshot?target=ID&file=/tmp/shot.png&format=png"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    filepath = request.query.get('file', '')
    fmt = request.query.get('format', 'png')
    if fmt not in ('png', 'jpeg'):
        return web.json_response({'error': f'Invalid format: {fmt}. Use png|jpeg'}, status=400)
    try:
        params: dict = {'format': fmt}
        if fmt == 'jpeg':
            params['quality'] = 80
        result = await cdp_command('Page.captureScreenshot', params, target_id=target_id)
        if 'error' in result and 'result' not in result:
            return web.json_response({'error': result.get('error')}, status=502)
        data_b64 = result.get('result', {}).get('data', '')
        if data_b64:
            img_data = base64.b64decode(data_b64)
            if filepath:
                try:
                    with open(filepath, 'wb') as f:
                        f.write(img_data)
                except OSError as e:
                    return web.json_response({'error': f'Failed to save screenshot: {e}'}, status=500)
                return web.json_response({'file': filepath, 'size': len(img_data)})
            else:
                return web.Response(body=img_data, content_type=f'image/{fmt}')
        return web.json_response({'error': 'No screenshot data returned'}, status=502)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_set_files(request: web.Request) -> web.Response:
    """Set file inputs: POST /setFiles?target=ID, body={"selector":"...","files":[...]}"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    body = await request.text()
    try:
        params = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON body'}, status=400)
    selector = params.get('selector', '')
    files = params.get('files', [])
    if not selector or not files:
        return web.json_response({'error': 'Need selector and files fields'}, status=400)
    try:
        result = await cdp_command('DOM.getDocument', {}, target_id=target_id)
        doc_node_id = result.get('result', {}).get('root', {}).get('nodeId')
        find_result = await cdp_command('DOM.querySelector', {
            'nodeId': doc_node_id, 'selector': selector
        }, target_id=target_id)
        node_id = find_result.get('result', {}).get('nodeId')
        if not node_id:
            return web.json_response({'error': f'Element not found: {selector}'}, status=400)
        await cdp_command('DOM.setFileInputFiles', {
            'nodeId': node_id, 'files': files
        }, target_id=target_id)
        return web.json_response({'success': True, 'files': len(files)})
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)
    except RuntimeError as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_health(request: web.Request) -> web.Response:
    """Health check: GET /health"""
    try:
        if _conn.is_connected:
            return web.json_response({
                'status': 'ok',
                'connected': True,
                'sessions': len(_conn.sessions),
                'chromePort': CHROME_PORT,
            })
        # Try to connect
        await _conn.connect()
        version = await chrome_http('/json/version')
        return web.json_response({
            'status': 'ok',
            'connected': True,
            'browser': version.get('Browser', 'unknown'),
            'sessions': len(_conn.sessions),
            'chromePort': CHROME_PORT,
        })
    except Exception:
        return web.json_response({
            'status': 'disconnected', 'connected': False, 'chromePort': CHROME_PORT,
        }, status=503)

async def handle_not_found(request: web.Request) -> web.Response:
    """404 handler with API listing."""
    return web.json_response({
        'error': 'Unknown endpoint',
        'endpoints': {
            '/health': 'GET - Health check',
            '/targets': 'GET - List all page tabs',
            '/new?url=': 'GET - Create new background tab (waits for load)',
            '/close?target=': 'GET - Close tab',
            '/navigate?target=&url=': 'GET - Navigate (waits for load)',
            '/back?target=': 'GET - Go back',
            '/info?target=': 'GET - Page title/URL/readyState',
            '/eval?target=': 'POST body=JS expression - Execute JS',
            '/click?target=': 'POST body=CSS selector - Click element',
            '/clickAt?target=': 'POST body=CSS selector - Real mouse click',
            '/scroll?target=&y=&direction=': 'GET - Scroll page',
            '/screenshot?target=&file=&format=': 'GET - Screenshot',
            '/setFiles?target=': 'POST body=JSON - Set file inputs',
        },
    }, status=404)

# --- Global error handling ---

def _handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical(f'Uncaught exception: {exc_value}', exc_info=(exc_type, exc_value, exc_tb))

def _handle_loop_exception(loop, context):
    exception = context.get('exception')
    message = context.get('message', 'Unknown error')
    if exception:
        logger.error(f'Unhandled asyncio exception: {message} — {exception}')
    else:
        logger.error(f'Unhandled asyncio error: {message}')

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get('/targets', handle_targets)
    app.router.add_get('/json', handle_targets)
    app.router.add_get('/json/version', handle_version)
    app.router.add_get('/new', handle_new)
    app.router.add_get('/close', handle_close)
    app.router.add_get('/info', handle_info)
    app.router.add_post('/eval', handle_eval)
    app.router.add_get('/navigate', handle_navigate)
    app.router.add_get('/back', handle_back)
    app.router.add_post('/click', handle_click)
    app.router.add_post('/clickAt', handle_click_at)
    app.router.add_get('/scroll', handle_scroll)
    app.router.add_get('/screenshot', handle_screenshot)
    app.router.add_post('/setFiles', handle_set_files)
    app.router.add_get('/health', handle_health)
    app.router.add_route('*', '/{path:.*}', handle_not_found)
    app.on_shutdown.append(on_shutdown)
    return app

async def on_shutdown(app: web.Application) -> None:
    """Clean up on shutdown."""
    global _shared_session
    if _conn._ws and not _conn._ws.closed:
        await _conn._ws.close()
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()
    logger.info('Shutdown complete')

async def main() -> None:
    """Entry point with port conflict detection and global error handling."""
    sys.excepthook = _handle_exception
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_handle_loop_exception)
    
    if not _check_port_available(PROXY_PORT, BRIDGE_HOST):
        try:
            import urllib.request
            with urllib.request.urlopen(f'http://{BRIDGE_HOST}:{PROXY_PORT}/health', timeout=2) as resp:
                if b'"ok"' in resp.read():
                    logger.info(f'CDP Bridge already running on port {PROXY_PORT}, exiting')
                    sys.exit(0)
        except Exception:
            pass
        logger.error(f'Port {PROXY_PORT} is already in use by another process')
        sys.exit(1)
    
    logger.info(f'Proxying {BRIDGE_HOST}:{PROXY_PORT} → {CHROME_HOST}:{CHROME_PORT}')
    app = create_app()
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(_shutdown(app, s)))
    
    web.run_app(app, host=BRIDGE_HOST, port=PROXY_PORT, print=None)

async def _shutdown(app: web.Application, sig: signal.Signals) -> None:
    """Graceful shutdown handler."""
    logger.info(f'Received signal {sig.name}, shutting down...')
    await app.shutdown()
    await app.cleanup()

if __name__ == '__main__':
    asyncio.run(main())