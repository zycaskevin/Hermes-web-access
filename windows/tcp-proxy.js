// tcp-proxy.js - Simple TCP proxy to expose Chrome CDP to WSL
// Listens on 0.0.0.0:9223 (configurable), forwards to 127.0.0.1:9222
//
// Security note: This proxy runs on the local network (WSL2 ↔ Windows).
// It has NO authentication or encryption — only use on trusted networks.
// For WSL2 this is acceptable as traffic stays on the host's virtual network.

const net = require('net');

const LISTEN_PORT = parseInt(process.env.TCP_PROXY_PORT || '9223', 10);
const TARGET_HOST = process.env.TCP_TARGET_HOST || '127.0.0.1';
const TARGET_PORT = parseInt(process.env.TCP_TARGET_PORT || '9222', 10);
const MAX_CONNECTIONS = parseInt(process.env.TCP_MAX_CONNECTIONS || '50', 10);
const IDLE_TIMEOUT_MS = parseInt(process.env.TCP_IDLE_TIMEOUT || '300000', 10); // 5 min default

let activeConnections = 0;

const server = net.createServer((clientSocket) => {
  if (activeConnections >= MAX_CONNECTIONS) {
    console.error(`[proxy] Max connections (${MAX_CONNECTIONS}) reached, rejecting`);
    clientSocket.destroy();
    return;
  }

  const targetSocket = net.connect(TARGET_PORT, TARGET_HOST, () => {
    activeConnections++;
    console.log(`[proxy] Connected: ${clientSocket.remoteAddress}:${clientSocket.remotePort} -> ${TARGET_HOST}:${TARGET_PORT} (${activeConnections} active)`);
  });
  
  clientSocket.pipe(targetSocket);
  targetSocket.pipe(clientSocket);
  
  // Idle timeout to prevent resource exhaustion
  const idleTimer = setTimeout(() => {
    console.log(`[proxy] Idle timeout, closing connection`);
    clientSocket.destroy();
    targetSocket.destroy();
  }, IDLE_TIMEOUT_MS);
  
  const resetIdle = () => idleTimer.refresh();
  clientSocket.on('data', resetIdle);
  targetSocket.on('data', resetIdle);
  
  const cleanup = () => {
    clearTimeout(idleTimer);
    if (activeConnections > 0) activeConnections--;
    targetSocket.destroy();
  };
  
  clientSocket.on('error', (err) => {
    console.error(`[proxy] Client error: ${err.message}`);
    cleanup();
  });
  targetSocket.on('error', (err) => {
    console.error(`[proxy] Target error: ${err.message}`);
    clientSocket.destroy();
    clearTimeout(idleTimer);
    if (activeConnections > 0) activeConnections--;
  });
  clientSocket.on('close', cleanup);
  targetSocket.on('close', () => {
    clearTimeout(idleTimer);
    clientSocket.destroy();
  });
});

server.listen(LISTEN_PORT, '0.0.0.0', () => {
  console.log(`[proxy] Listening on 0.0.0.0:${LISTEN_PORT} -> ${TARGET_HOST}:${TARGET_PORT}`);
  console.log(`[proxy] WSL can connect via host IP:${LISTEN_PORT}`);
  console.log(`[proxy] Max connections: ${MAX_CONNECTIONS}, Idle timeout: ${IDLE_TIMEOUT_MS}ms`);
});

server.on('error', (err) => {
  console.error(`[proxy] Server error: ${err.message}`);
  process.exit(1);
});