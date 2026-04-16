#!/bin/bash
# CDP Bridge startup script for Hermes Agent
# Supports WSL2 (auto-detects gateway) and native Linux/macOS

CDP_BRIDGE_PIDFILE="/tmp/cdp-bridge.pid"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CDP_BRIDGE_SCRIPT="$SCRIPT_DIR/cdp-bridge.py"

# Auto-detect environment
# Linux/macOS: Chrome is local → 127.0.0.1:9222
# Windows (WSL2): Chrome is on Windows host → <gateway>:9223
if grep -qi microsoft /proc/version 2>/dev/null; then
    GATEWAY_IP=$(ip route show default 2>/dev/null | grep -oP 'via \K[\d.]+')
    GATEWAY_IP=${GATEWAY_IP:-127.0.0.1}
    DEFAULT_CHROME_HOST="$GATEWAY_IP"
    DEFAULT_CHROME_PORT="9223"
else
    DEFAULT_CHROME_HOST="127.0.0.1"
    DEFAULT_CHROME_PORT="9222"
fi

start() {
    if [ -f "$CDP_BRIDGE_PIDFILE" ] && kill -0 "$(cat "$CDP_BRIDGE_PIDFILE")" 2>/dev/null; then
        echo "CDP Bridge already running (PID $(cat "$CDP_BRIDGE_PIDFILE"))"
        return 0
    fi
    
    export CHROME_HOST="${CHROME_HOST:-$DEFAULT_CHROME_HOST}"
    export CHROME_PORT="${CHROME_PORT:-$DEFAULT_CHROME_PORT}"
    export CDP_PROXY_PORT="${CDP_PROXY_PORT:-3456}"
    
    echo "Chrome at $CHROME_HOST:$CHROME_PORT → Bridge on :$CDP_PROXY_PORT"
    python3 "$CDP_BRIDGE_SCRIPT" &
    echo $! > "$CDP_BRIDGE_PIDFILE"
    sleep 2
    
    if curl -s --max-time 3 http://localhost:$CDP_PROXY_PORT/health 2>/dev/null | grep -q '"ok"'; then
        echo "CDP Bridge started ✓"
    else
        echo "Warning: CDP Bridge not responding. Check Chrome CDP + tcp-proxy."
    fi
}

stop() {
    if [ -f "$CDP_BRIDGE_PIDFILE" ]; then
        kill "$(cat "$CDP_BRIDGE_PIDFILE")" 2>/dev/null
        rm -f "$CDP_BRIDGE_PIDFILE"
        echo "CDP Bridge stopped"
    else
        echo "CDP Bridge not running"
    fi
}

status() {
    PORT="${CDP_PROXY_PORT:-3456}"
    health=$(curl -s --max-time 3 "http://localhost:$PORT/health" 2>/dev/null)
    if echo "$health" | grep -q '"ok"'; then
        browser=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('browser','?'))" 2>/dev/null)
        echo "CDP Bridge: running ✓ ($browser)"
    else
        echo "CDP Bridge: not running"
    fi
}

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    status)  status ;;
    restart) stop; sleep 1; start ;;
    *)       echo "Usage: $0 {start|stop|status|restart}" ;;
esac