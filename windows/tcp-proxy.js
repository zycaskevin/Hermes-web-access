// tcp-proxy.js - Simple TCP proxy to expose Chrome CDP to WSL
// Listens on 0.0.0.0:9223 (configurable), forwards to 127.0.0.1:9222
const net = require('net');

const LISTEN_PORT = parseInt(process.env.TCP_PROXY_PORT || '9223', 10);
const TARGET_HOST = process.env.TCP_TARGET_HOST || '127.0.0.1';
const TARGET_PORT = parseInt(process.env.TCP_TARGET_PORT || '9222', 10);

const server = net.createServer((clientSocket) => {
  const targetSocket = net.connect(TARGET_PORT, TARGET_HOST, () => {
    console.log(`[proxy] Connected: ${clientSocket.remoteAddress}:${clientSocket.remotePort} -> ${TARGET_HOST}:${TARGET_PORT}`);
  });
  
  clientSocket.pipe(targetSocket);
  targetSocket.pipe(clientSocket);
  
  clientSocket.on('error', (err) => {
    console.error(`[proxy] Client error: ${err.message}`);
    targetSocket.destroy();
  });
  targetSocket.on('error', (err) => {
    console.error(`[proxy] Target error: ${err.message}`);
    clientSocket.destroy();
  });
  clientSocket.on('close', () => targetSocket.destroy());
  targetSocket.on('close', () => clientSocket.destroy());
});

server.listen(LISTEN_PORT, '0.0.0.0', () => {
  console.log(`[proxy] Listening on 0.0.0.0:${LISTEN_PORT} -> ${TARGET_HOST}:${TARGET_PORT}`);
  console.log(`[proxy] WSL can connect via host IP:${LISTEN_PORT}`);
});

server.on('error', (err) => {
  console.error(`[proxy] Server error: ${err.message}`);
  process.exit(1);
});