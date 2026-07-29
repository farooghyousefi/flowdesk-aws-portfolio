import http from "node:http";
import { WebSocketServer } from "ws";
import { TradovateConnector, createSimulationMessages } from "./provider.mjs";
import {
  MAX_REQUEST_BYTES,
  PROVIDER_PORT,
  buildProviderCapabilities,
  createRateLimiter,
  getCapabilityDegradationReasons,
  getProviderMode,
  isAllowedOrigin,
  isLocalAddress,
  sanitizeLog
} from "./security.mjs";

const provider = new TradovateConnector();
const rateLimit = createRateLimiter({ maxRequests: 120, windowMs: 60_000 });
const clients = new Set();

function sendJson(response, status, payload) {
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "access-control-allow-origin": "http://localhost:3000",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type"
  });
  response.end(JSON.stringify(payload));
}

function rejectIfNotLocal(request, response) {
  const address = request.socket.remoteAddress;
  if (!isLocalAddress(address) || !isAllowedOrigin(request.headers.origin)) {
    sendJson(response, 403, { ok: false, error: "localhost_only" });
    return true;
  }
  if (!rateLimit(address)) {
    sendJson(response, 429, { ok: false, error: "rate_limited" });
    return true;
  }
  return false;
}

const server = http.createServer((request, response) => {
  if (request.method === "OPTIONS") {
    sendJson(response, 204, {});
    return;
  }

  if (rejectIfNotLocal(request, response)) return;

  if (request.method === "GET" && request.url === "/health") {
    sendJson(response, 200, {
      ok: true,
      service: "tradovate-provider",
      mode: getProviderMode(),
      connectionState: provider.getConnectionState(),
      lastHeartbeatAt: provider.lastHeartbeatAt,
      orderExecution: "disabled"
    });
    return;
  }

  if (request.method === "GET" && request.url === "/capabilities") {
    sendJson(response, 200, {
      ok: true,
      capabilities: buildProviderCapabilities(),
      degradationReasons: getCapabilityDegradationReasons()
    });
    return;
  }

  if (request.method === "GET" && request.url === "/status") {
    sendJson(response, 200, { ok: true, status: provider.getStatus() });
    return;
  }

  if (request.method === "POST" && request.url === "/simulate/tick" && getProviderMode() === "simulation") {
    let bytes = 0;
    request.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > MAX_REQUEST_BYTES) request.destroy();
    });
    request.on("end", () => {
      createSimulationMessages().forEach(broadcast);
      sendJson(response, 202, { ok: true });
    });
    return;
  }

  sendJson(response, 404, { ok: false, error: "not_found" });
});

const wss = new WebSocketServer({ noServer: true });

server.on("upgrade", (request, socket, head) => {
  if (request.url !== "/stream" || !isLocalAddress(request.socket.remoteAddress) || !isAllowedOrigin(request.headers.origin)) {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
});

wss.on("connection", (ws) => {
  clients.add(ws);
  ws.send(JSON.stringify({ version: 1, type: "provider_status", timestamp: new Date().toISOString(), ...provider.getStatus() }));
  ws.on("close", () => clients.delete(ws));
});

function broadcast(message) {
  const payload = JSON.stringify(message);
  for (const client of clients) {
    if (client.readyState === client.OPEN) client.send(payload);
  }
}

provider.onQuote(broadcast);
provider.onBar(broadcast);
provider.onDepth(broadcast);
provider.onAccount(broadcast);
provider.onPosition(broadcast);
provider.onOrder(broadcast);
provider.onExecution(broadcast);

provider
  .connect()
  .then(() => console.log(JSON.stringify(sanitizeLog({ msg: "tradovate_provider_started", port: PROVIDER_PORT, status: provider.getStatus() }))))
  .catch((error) => {
    console.error(JSON.stringify(sanitizeLog({ msg: "tradovate_provider_degraded", error: error.message, status: provider.getStatus() })));
  })
  .finally(() => {
    server.listen(PROVIDER_PORT, "127.0.0.1");
  });

process.on("SIGINT", async () => {
  await provider.disconnect();
  server.close(() => process.exit(0));
});
