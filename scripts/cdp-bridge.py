#!/usr/bin/env python3
"""WSL-to-Windows CDP Bridge - Full Chrome DevTools Protocol proxy for Hermes Agent.
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
import os
import sys
import base64

# Config
PROXY_PORT = int(os.environ.get('CDP_PROXY_PORT', '3456'))

# Auto-detect Chrome CDP host
# WSL2: need Windows gateway IP + tcp-proxy port (usually 9223)
# Native: just localhost:9222
if os.environ.get('CHROME_HOST'):
    CHROME_HOST = os.environ['CHROME_HOST']
elif os.path.exists('/proc/version') and 'microsoft' in open('/proc/version').read().lower():
    # WSL2 detected - try to get Windows gateway IP
    try:
        import subprocess
        result = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=5)
        CHROME_HOST = result.stdout.strip().split()[-1] if result.stdout.strip() else '127.0.0.1'
    except:
        CHROME_HOST = '127.0.0.1'
else:
    CHROME_HOST = '127.0.0.1'

CHROME_PORT = int(os.environ.get('CHROME_PORT', '9223' if CHROME_HOST != '127.0.0.1' else '9222'))

# CDP session management
_cmd_id = 0
_sessions = {}  # targetId -> sessionId

async def chrome_http(path, method='GET'):
    """Make HTTP request to Chrome CDP."""
    url = f'http://{CHROME_HOST}:{CHROME_PORT}{path}'
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except:
                return {'_raw': text, '_status': resp.status}

async def cdp_command(method, params=None, target_id=None, timeout=30):
    """Send a CDP command via WebSocket, connecting to the target's WS URL directly."""
    global _cmd_id
    
    # Get WebSocket URL for target
    ws_url = None
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
            raise Exception(f"Target {target_id} not found and no browser WS URL")
    
    # Connect and send command
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=timeout)) as ws:
            _cmd_id += 1
            msg = {'id': _cmd_id, 'method': method}
            if params:
                msg['params'] = params
            
            await ws.send_str(json.dumps(msg))
            
            # Wait for response matching our command ID
            async for ws_msg in ws:
                if ws_msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(ws_msg.data)
                    if data.get('id') == _cmd_id:
                        return data
                    # Skip events
                elif ws_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    raise Exception("WebSocket closed")
    
    raise Exception("Command timed out")

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
        # Wait a moment for page to start loading
        await asyncio.sleep(1)
        return web.json_response(data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_close(request):
    """Close tab: GET /close?target=ID"""
    target_id = request.query.get('target', '')
    try:
        data = await chrome_http(f'/json/close/{target_id}')
        return web.json_response(data)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_info(request):
    """Page info: GET /info?target=ID"""
    target_id = request.query.get('target', '')
    try:
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
            'awaitPromise': False
        }, target_id=target_id)
        
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_navigate(request):
    """Navigate: GET /navigate?target=ID&url=URL"""
    target_id = request.query.get('target', '')
    url = request.query.get('url', '')
    try:
        result = await cdp_command('Page.navigate', {'url': url}, target_id=target_id)
        await asyncio.sleep(2)  # Wait for navigation
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_back(request):
    """Go back: GET /back?target=ID"""
    target_id = request.query.get('target', '')
    try:
        result = await cdp_command('Page.getNavigationHistory', target_id=target_id)
        entries = result.get('result', {}).get('entries', [])
        current = result.get('result', {}).get('currentIndex', 0)
        if current > 0:
            await cdp_command('Page.navigateToHistoryEntry', {'entryId': entries[current-1]['id']}, target_id=target_id)
            await asyncio.sleep(1)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_click(request):
    """Click element: POST /click?target=ID, body=selector"""
    target_id = request.query.get('target', '')
    selector = await request.text()
    try:
        js = f'''
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (el) {{ el.click(); return {{clicked: true, tag: el.tagName}}; }}
                return {{clicked: false, error: "Element not found: " + {json.dumps(selector)}}};
            }})()
        '''
        result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True
        }, target_id=target_id)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_click_at(request):
    """Real mouse click: POST /clickAt?target=ID, body=selector"""
    target_id = request.query.get('target', '')
    selector = await request.text()
    try:
        # Get element position first
        js = f'''
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{error: "Element not found"}};
                const rect = el.getBoundingClientRect();
                return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2}};
            }})()
        '''
        pos_result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True
        }, target_id=target_id)
        
        pos = pos_result.get('result', {}).get('result', {}).get('value', {})
        if 'error' in pos:
            return web.json_response(pos_result)
        
        x, y = pos.get('x', 0), pos.get('y', 0)
        # Dispatch mouse events
        for typ, btn in [('mousePressed', 'left'), ('mouseReleased', 'left')]:
            await cdp_command('Input.dispatchMouseEvent', {
                'type': typ, 'x': x, 'y': y, 'button': btn, 'clickCount': 1
            }, target_id=target_id)
        
        return web.json_response({'clicked': True, 'x': x, 'y': y})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_scroll(request):
    """Scroll: GET /scroll?target=ID&y=N or &direction=bottom"""
    target_id = request.query.get('target', '')
    y = request.query.get('y', '')
    direction = request.query.get('direction', '')
    try:
        if direction == 'bottom':
            js = 'window.scrollTo(0, document.body.scrollHeight)'
        elif y:
            js = f'window.scrollBy(0, {int(y)} - window.scrollY)' if int(y) < 10000 else f'window.scrollTo(0, {int(y)})'
        else:
            js = 'window.scrollBy(0, 500)'
        
        result = await cdp_command('Runtime.evaluate', {
            'expression': js, 'returnByValue': True
        }, target_id=target_id)
        await asyncio.sleep(0.5)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_screenshot(request):
    """Screenshot: GET /screenshot?target=ID&file=/tmp/shot.png"""
    target_id = request.query.get('target', '')
    filepath = request.query.get('file', '/tmp/screenshot.png')
    try:
        result = await cdp_command('Page.captureScreenshot', {
            'format': 'png'
        }, target_id=target_id)
        
        data_b64 = result.get('result', {}).get('data', '')
        if data_b64:
            img_data = base64.b64decode(data_b64)
            with open(filepath, 'wb') as f:
                f.write(img_data)
            result['file'] = filepath
            result['size'] = len(img_data)
            # Remove the base64 data from JSON response (too large)
            result['result'].pop('data', None)
        
        return web.json_response(result)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_set_files(request):
    """Set file inputs: POST /setFiles?target=ID, body={"selector":"...","files":[...]}"""
    target_id = request.query.get('target', '')
    body = await request.text()
    try:
        params = json.loads(body)
        result = await cdp_command('DOM.getDocument', {}, target_id=target_id)
        doc_node_id = result.get('result', {}).get('root', {}).get('nodeId')
        
        # Find the file input
        find_result = await cdp_command('DOM.querySelector', {
            'nodeId': doc_node_id, 'selector': params['selector']
        }, target_id=target_id)
        node_id = find_result.get('result', {}).get('nodeId')
        
        if node_id:
            await cdp_command('DOM.setFileInputFiles', {
                'nodeId': node_id, 'files': params['files']
            }, target_id=target_id)
        
        return web.json_response({'set': True})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=502)

async def handle_health(request):
    """Health check: GET /health"""
    try:
        data = await chrome_http('/json/version')
        return web.json_response({'status': 'ok', 'browser': data.get('Browser', 'unknown')})
    except:
        return web.json_response({'status': 'disconnected'}, status=503)

def create_app():
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
    return app

if __name__ == '__main__':
    print(f'[CDP Bridge] Proxying 0.0.0.0:{PROXY_PORT} → {CHROME_HOST}:{CHROME_PORT}', flush=True)
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=PROXY_PORT, print=None)