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
import sys
import base64

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
NAV_POLL_INTERVAL = 0.5  # seconds between readyState polls
NAV_POLL_TIMEOUT = 15    # max seconds to wait for page load

# --- Auto-detect Chrome CDP host ---
# Linux/macOS: Chrome is local → 127.0.0.1:9222
# Windows (WSL2): Chrome is on Windows host → <gateway>:9223
if os.environ.get('CHROME_HOST'):
    CHROME_HOST = os.environ['CHROME_HOST']
elif os.environ.exists('/proc/version') if hasattr(os.environ, 'exists') else (os.path.exists('/proc/version') and 'microsoft' in open('/proc/version').read().lower()):
    # WSL2 — get Windows gateway IP
    try:
        import subprocess
        result = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=5)
        CHROME_HOST = result.stdout.strip().split()[-1] if result.stdout.strip() else '127.0.0.1'
    except Exception:
        CHROME_HOST = '127.0.0.1'
    CHROME_PORT = int(os.environ.get('CHROME_PORT', '9223'))
else:
    CHROME_HOST = '127.0.0.1'
    CHROME_PORT = int(os.environ.get('CHROME_PORT', '9222'))

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
                # Skip events
            elif ws_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                raise RuntimeError(f"WebSocket closed while waiting for {method}")
    
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
    except Exception:
        logger.debug(f'Port guard failed (non-fatal) for session {session_id[:8]}...')

async def wait_for_load(target_id: str, timeout_ms: int = NAV_POLL_TIMEOUT * 1000) -> str:
    """Poll readyState until 'complete' or timeout."""
    try:
        await cdp_command('Page.enable', {}, target_id=target_id)
    except Exception:
        pass  # Page may already be enabled
    
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        try:
            result = await cdp_command('Runtime.evaluate', {
                'expression': 'document.readyState',
                'returnByValue': True,
            }, target_id=target_id)
            if result.get('result', {}).get('result', {}).get('value') == 'complete':
                return 'complete'
        except Exception:
            pass
        await asyncio.sleep(NAV_POLL_INTERVAL)
    return 'timeout'

# --- HTTP API Handlers (compatible with web-access cdp-proxy.mjs) ---

async def handle_targets(request):
    """List Chrome targets: GET /targets"""
    try:
        data = await chrome_http('/json')
        return web.json_response(data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_version(request):
    """Chrome version: GET /json/version"""
    try:
        data = await chrome_http('/json/version')
        return web.json_response(data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_new(request):
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
                except Exception:
                    await asyncio.sleep(1)  # fallback
        return web.json_response(data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_close(request):
    """Close tab: GET /close?target=ID"""
    target_id = request.query.get('target', '')
    try:
        data = await chrome_http(f'/json/close/{target_id}')
        # Clean up session mapping
        _sessions.pop(target_id, None)
        return web.json_response(data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_info(request):
    """Page info: GET /info?target=ID"""
    target_id = request.query.get('target', '')
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
        for t in targets:
            if t.get('id') == target_id:
                return web.json_response(t)
        return web.json_response({'error': 'Target not found'}, status=404)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_eval(request):
    """Execute JS: POST /eval?target=ID, body=expression"""
    target_id = request.query.get('target', '')
    expression = await request.text()
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
            return web.json_response({'error': inner['exceptionDetails'].get('text', 'JS exception')}, status=400)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_navigate(request):
    """Navigate: GET /navigate?target=ID&url=URL"""
    target_id = request.query.get('target', '')
    url = request.query.get('url', '')
    try:
        result = await cdp_command('Page.navigate', {'url': url}, target_id=target_id)
        # Poll for page load instead of fixed sleep
        await wait_for_load(target_id)
        return web.json_response(result.get('result', result))
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_back(request):
    """Go back: GET /back?target=ID"""
    target_id = request.query.get('target', '')
    try:
        # Use history.back() + waitForLoad like upstream
        await cdp_command('Runtime.evaluate', {'expression': 'history.back()'}, target_id=target_id)
        await wait_for_load(target_id)
        return web.json_response({'ok': True})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_click(request):
    """Click element: POST /click?target=ID, body=selector"""
    target_id = request.query.get('target', '')
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
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_click_at(request):
    """Real mouse click: POST /clickAt?target=ID, body=selector"""
    target_id = request.query.get('target', '')
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
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_scroll(request):
    """Scroll: GET /scroll?target=ID&y=N&direction=down|up|top|bottom"""
    target_id = request.query.get('target', '')
    y_raw = request.query.get('y', '')
    direction = request.query.get('direction', 'down')
    try:
        if direction == 'top':
            js = 'window.scrollTo(0, 0); "scrolled to top"'
        elif direction == 'bottom':
            js = 'window.scrollTo(0, document.body.scrollHeight); "scrolled to bottom"'
        elif direction == 'up':
            # Validate y parameter
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
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_screenshot(request):
    """Screenshot: GET /screenshot?target=ID&file=/tmp/shot.png&format=png"""
    target_id = request.query.get('target', '')
    filepath = request.query.get('file', '')
    fmt = request.query.get('format', 'png')
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
                with open(filepath, 'wb') as f:
                    f.write(img_data)
                # Remove large base64 from response
                resp_data = {k: v for k, v in result.items() if k != 'result'}
                resp_data['file'] = filepath
                resp_data['size'] = len(img_data)
                return web.json_response(resp_data)
            else:
                # Return raw image binary (matching upstream behavior)
                return web.Response(body=img_data, content_type=f'image/{fmt}')
        
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_set_files(request):
    """Set file inputs: POST /setFiles?target=ID, body={"selector":"...","files":[...]}"""
    target_id = request.query.get('target', '')
    body = await request.text()
    try:
        params = json.loads(body)
        selector = params.get('selector', '')
        files = params.get('files', [])
        
        if not selector or not files:
            return web.json_response({'error': 'Need selector and files fields'}, status=400)
        
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
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON body'}, status=400)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_health(request):
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
    except Exception:
        return web.json_response({'status': 'disconnected', 'connected': False, 'chromePort': CHROME_PORT}, status=503)

async def handle_not_found(request):
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

async def on_shutdown(app):
    """Clean up shared session on shutdown."""
    global _shared_session
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()

if __name__ == '__main__':
    logger.info(f'Proxying {BRIDGE_HOST}:{PROXY_PORT} → {CHROME_HOST}:{CHROME_PORT}')
    app = create_app()
    web.run_app(app, host=BRIDGE_HOST, port=PROXY_PORT, print=None)