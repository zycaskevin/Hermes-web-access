#!/usr/bin/env python3
"""CDP Bridge - Full Chrome DevTools Protocol proxy for Hermes Agent.
Bridges all CDP operations from WSL to Windows Chrome via TCP proxy.

Usage:
  # WSL2 (auto-detected):
  python3 cdp-bridge.py
  
  # Native Linux/macOS:
  CHROME_HOST=127.0.0.1 CHROME_PORT=9222 python3 cdp-bridge.py

Architecture (WSL2):
  WSL Agent → localhost:3456 (this bridge) → <gateway>:9223 (Windows tcp-proxy.js) → 127.0.0.1:9222 (Chrome CDP)

Architecture (Native):
  Agent → localhost:3456 (this bridge) → 127.0.0.1:9222 (Chrome CDP)
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
NAV_POLL_INTERVAL = float(os.environ.get('NAV_POLL_INTERVAL', '0.5'))  # seconds between readyState polls
NAV_POLL_TIMEOUT = int(os.environ.get('NAV_POLL_TIMEOUT', '15'))  # max seconds to wait for page load

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

# --- Chrome auto-discovery (match upstream's DevToolsActivePort logic) ---
def _discover_chrome_port() -> int | None:
    """Try to discover Chrome's debug port from DevToolsActivePort file.
    
    Returns port number if found and reachable, None otherwise.
    """
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
    
    # Fallback: probe common ports
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
    """Check if a port is available for binding (not already in use)."""
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

# --- Shared HTTP session (performance) ---
_shared_session: aiohttp.ClientSession | None = None

async def get_session() -> aiohttp.ClientSession:
    """Get or create the shared aiohttp ClientSession."""
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession()
    return _shared_session

# --- CDP session management ---
_cmd_id = 0  # monotonically increasing command ID (safe in single-threaded asyncio)
_sessions: dict[str, str] = {}  # targetId -> sessionId
_port_guard_sessions: set[str] = set()  # sessions with port guard enabled

async def chrome_http(path: str, method: str = 'GET') -> dict | list:
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

async def cdp_command(method: str, params: dict | None = None, target_id: str | None = None, timeout: int | None = None) -> dict:
    """Send a CDP command via WebSocket, connecting to the target's WS URL directly."""
    global _cmd_id
    
    timeout = timeout or CDP_WS_TIMEOUT
    
    # Get WebSocket URL for target
    ws_url: str | None = None
    if target_id:
        targets = await chrome_http('/json')
        if isinstance(targets, list):
            for t in targets:
                if t.get('id') == target_id:
                    ws_url = t.get('webSocketDebuggerUrl', '')
                    break
    
    if not ws_url:
        # Fallback to browser-level WebSocket  
        version = await chrome_http('/json/version')
        ws_url = version.get('webSocketDebuggerUrl', '')
        if not ws_url:
            raise RuntimeError(f"Target {target_id} not found and no browser WS URL")
    
    # Connect and send command
    session = await get_session()
    try:
        async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=timeout)) as ws:
            _cmd_id += 1
            current_id = _cmd_id
            msg: dict = {'id': current_id, 'method': method}
            if params:
                msg['params'] = params
            
            await ws.send_str(json.dumps(msg))
            
            # Wait for response matching our command ID
            async for ws_msg in ws:
                if ws_msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(ws_msg.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get('id') == current_id:
                        if 'error' in data:
                            logger.warning(f'CDP error for {method}: {data["error"]}')
                        return data
                    # Skip CDP events
                elif ws_msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"WebSocket error while waiting for {method}: {ws.exception()}")
                elif ws_msg.type == aiohttp.WSMsgType.CLOSED:
                    raise RuntimeError(f"WebSocket closed while waiting for {method}")
    except aiohttp.WSServerHandshakeError as e:
        raise RuntimeError(f"WebSocket handshake failed for {method}: {e}") from e
    except aiohttp.ClientError as e:
        raise RuntimeError(f"WebSocket connection failed for {method}: {e}") from e
    
    raise RuntimeError(f"Command {method} timed out after {timeout}s")

async def ensure_session(target_id: str) -> str:
    """Get or create a CDP session for the target."""
    if target_id in _sessions:
        return _sessions[target_id]
    
    result = await cdp_command('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
    session_id = result.get('result', {}).get('sessionId')
    if not session_id:
        raise RuntimeError(f"Failed to attach to target {target_id}: {result}")
    
    _sessions[target_id] = session_id
    await enable_port_guard(target_id, session_id)
    return session_id

async def enable_port_guard(target_id: str, session_id: str) -> None:
    """Intercept page requests to Chrome's debug port (anti-detection).
    
    Websites can probe localhost:{CHROME_PORT} to detect CDP automation.
    We intercept these requests and reject them, matching upstream's behavior.
    """
    if session_id in _port_guard_sessions:
        return
    try:
        await cdp_command('Fetch.enable', {
            'patterns': [
                {'urlPattern': f'http://127.0.0.1:{CHROME_PORT}/*', 'requestStage': 'Request'},
                {'urlPattern': f'http://localhost:{CHROME_PORT}/*', 'requestStage': 'Request'},
            ]
        }, target_id=target_id)
        _port_guard_sessions.add(session_id)
        logger.debug(f'Port guard enabled for session {session_id[:8]}...')
    except RuntimeError:
        logger.debug(f'Port guard failed (non-fatal) for session {session_id[:8]}...')

async def wait_for_load(target_id: str, timeout_ms: int | None = None) -> str:
    """Poll readyState until 'complete' or timeout."""
    timeout_ms = timeout_ms or NAV_POLL_TIMEOUT * 1000
    try:
        await cdp_command('Page.enable', {}, target_id=target_id)
    except RuntimeError:
        pass  # Page may already be enabled
    
    loop = asyncio.get_event_loop()
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

# --- HTTP API Handlers (compatible with web-access cdp-proxy.mjs) ---

class CDPError(Exception):
    """Error from CDP command execution."""
    pass

async def handle_targets(request: web.Request) -> web.Response:
    """List Chrome targets: GET /targets"""
    try:
        data = await chrome_http('/json')
        return web.json_response(data)
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
        data = await chrome_http(f'/json/new?{url}', method='PUT')
        # Wait for page to actually load (instead of fixed sleep)
        if url != 'about:blank':
            target_id = data.get('id')
            if target_id:
                try:
                    await wait_for_load(target_id)
                except (RuntimeError, ConnectionError):
                    await asyncio.sleep(1)  # fallback
        return web.json_response(data)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_close(request: web.Request) -> web.Response:
    """Close tab: GET /close?target=ID"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    try:
        data = await chrome_http(f'/json/close/{target_id}')
        # Clean up session mapping
        _sessions.pop(target_id, None)
        return web.json_response(data)
    except (ConnectionError, TimeoutError) as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_info(request: web.Request) -> web.Response:
    """Page info: GET /info?target=ID"""
    target_id = request.query.get('target', '')
    if not target_id:
        return web.json_response({'error': 'Missing target parameter'}, status=400)
    try:
        # Return runtime-evaluated info (title, url, readyState) like upstream
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
        # Fallback: query /json for basic target info
        targets = await chrome_http('/json')
        if isinstance(targets, list):
            for t in targets:
                if t.get('id') == target_id:
                    return web.json_response(t)
        return web.json_response({'error': 'Target not found'}, status=404)
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
            'awaitPromise': True,  # Match upstream: support async expressions
        }, target_id=target_id)
        
        # Better response formatting (match upstream API shape)
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
        # Poll for page load instead of fixed sleep
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
        # Use history.back() + waitForLoad like upstream
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
        # Dispatch mouse events
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
        else:  # down (default)
            try:
                y_val = abs(int(y_raw)) if y_raw else 500
            except ValueError:
                return web.json_response({'error': f'Invalid y value: {y_raw}'}, status=400)
            js = f'window.scrollBy(0, {y_val}); "scrolled down {y_val}px"'
        
        result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True,
        }, target_id=target_id)
        # Wait for lazy-load to trigger (match upstream's 800ms)
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
        
        # Check for CDP error
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
                resp_data = {'file': filepath, 'size': len(img_data)}
                return web.json_response(resp_data)
            else:
                # Return raw image binary (matching upstream behavior)
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
        data = await chrome_http('/json/version')
        return web.json_response({
            'status': 'ok',
            'connected': True,
            'browser': data.get('Browser', 'unknown'),
            'sessions': len(_sessions),
            'chromePort': CHROME_PORT,
        })
    except (ConnectionError, TimeoutError, aiohttp.ClientError):
        return web.json_response({
            'status': 'disconnected',
            'connected': False,
            'chromePort': CHROME_PORT,
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
    """Handle uncaught exceptions (equivalent to Node's uncaughtException)."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical(f'Uncaught exception: {exc_value}', exc_info=(exc_type, exc_value, exc_tb))

def _handle_loop_exception(loop, context):
    """Handle unhandled asyncio exceptions (equivalent to Node's unhandledRejection)."""
    exception = context.get('exception')
    message = context.get('message', 'Unknown error')
    if exception:
        logger.error(f'Unhandled asyncio exception: {message} — {exception}')
    else:
        logger.error(f'Unhandled asyncio error: {message}')

def create_app() -> web.Application:
    app = web.Application()
    # Target management
    app.router.add_get('/targets', handle_targets)
    app.router.add_get('/json', handle_targets)
    app.router.add_get('/json/version', handle_version)
    app.router.add_get('/new', handle_new)
    app.router.add_get('/close', handle_close)
    app.router.add_get('/info', handle_info)
    # Page operations  
    app.router.add_post('/eval', handle_eval)
    app.router.add_get('/navigate', handle_navigate)
    app.router.add_get('/back', handle_back)
    app.router.add_post('/click', handle_click)
    app.router.add_post('/clickAt', handle_click_at)
    app.router.add_get('/scroll', handle_scroll)
    app.router.add_get('/screenshot', handle_screenshot)
    app.router.add_post('/setFiles', handle_set_files)
    # Health
    app.router.add_get('/health', handle_health)
    # Catch-all 404
    app.router.add_route('*', '/{path:.*}', handle_not_found)
    # Cleanup on shutdown
    app.on_shutdown.append(on_shutdown)
    return app

async def on_shutdown(app: web.Application) -> None:
    """Clean up shared session on shutdown."""
    global _shared_session
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()
        logger.info('Shared session closed')

async def main() -> None:
    """Entry point with port conflict detection and global error handling."""
    # Install global error handlers
    sys.excepthook = _handle_exception
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_handle_loop_exception)
    
    # Port conflict detection
    if not _check_port_available(PROXY_PORT, BRIDGE_HOST):
        # Check if existing instance is healthy
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
    
    # Graceful shutdown on SIGTERM/SIGINT
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown(app, sig)))
    
    web.run_app(app, host=BRIDGE_HOST, port=PROXY_PORT, print=None)

async def _shutdown(app: web.Application, sig: signal.Signals) -> None:
    """Graceful shutdown handler."""
    logger.info(f'Received signal {sig.name}, shutting down...')
    await app.shutdown()
    await app.cleanup()

if __name__ == '__main__':
    asyncio.run(main())