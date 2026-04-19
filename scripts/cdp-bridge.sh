#!/bin/bash
# CDP Bridge startup script for Hermes Agent
# Supports WSL2 (auto-detects gateway) and native Linux/macOS

set -euo pipefail

# Use XDG_RUNTIME_DIR if available (more secure than /tmp)
PID_DIR="${XDG_RUNTIME_DIR:-/tmp}"
CDP_BRIDGE_PIDFILE="$PID_DIR/cdp-bridge.pid"
LOG_FILE="${XDG_RUNTIME_DIR:-/tmp}/cdp-bridge.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CDP_BRIDGE_SCRIPT="$SCRIPT_DIR/cdp-bridge.py"

# Auto-detect environment
if grep -qi microsoft /proc/version 2>/dev/null; then
    GATEWAY_IP=$(ip route show default 2>/dev/null | grep -oP 'via \K[\d.]+')
    GATEWAY_IP="${GATEWAY_IP:-127.0.0.1}"
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
    
    # Verify python3 is available
    if ! command -v python3 &>/dev/null; then
        echo "ERROR: python3 not found. Install: apt install python3 / brew install python3"
        return 1
    fi
    
    export CHROME_HOST="${CHROME_HOST:-$DEFAULT_CHROME_HOST}"
    export CHROME_PORT="${CHROME_PORT:-$DEFAULT_CHROME_PORT}"
    export CDP_PROXY_PORT="${CDP_PROXY_PORT:-3456}"
    export CDP_BRIDGE_HOST="${CDP_BRIDGE_HOST:-127.0.0.1}"
    
    echo "Chrome at $CHROME_HOST:$CHROME_PORT → Bridge on $CDP_BRIDGE_HOST:$CDP_PROXY_PORT"
    python3 "$CDP_BRIDGE_SCRIPT" >> "$LOG_FILE" 2>&1 &
    echo $! > "$CDP_BRIDGE_PIDFILE"
    sleep 2
    
    if curl -s --max-time 3 "http://localhost:$CDP_PROXY_PORT/health" 2>/dev/null | grep -q '"ok"'; then
        echo "CDP Bridge started ✓ (log: $LOG_FILE)"
    else
        echo "Warning: CDP Bridge not responding. Check log: $LOG_FILE"
    fi
}

stop() {
    if [ -f "$CDP_BRIDGE_PIDFILE" ]; then
        kill "$(cat "$CDP_BRIDGE_PIDFILE")" 2>/dev/null || true
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
        sessions=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sessions',0))" 2>/dev/null)
        echo "CDP Bridge: running ✓ ($browser, $sessions sessions)"
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