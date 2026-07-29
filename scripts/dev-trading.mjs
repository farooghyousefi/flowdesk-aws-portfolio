import { spawn } from "node:child_process";
import net from "node:net";

const children = [];
let stopping = false;

function available(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => server.close(() => resolve(true)));
    server.listen(port, "127.0.0.1");
  });
}

async function choosePort(start) {
  for (let port = start; port < start + 20; port += 1) {
    if (await available(port)) return port;
  }
  throw new Error(`No free local port near ${start}.`);
}

function run(command, args, env = {}) {
  const child = spawn(command, args, {
    cwd: process.cwd(),
    env: { ...process.env, ...env },
    stdio: "inherit",
    detached: process.platform !== "win32"
  });
  children.push(child);
  child.once("exit", (code) => {
    if (!stopping && code !== 0) {
      console.error(`${command} exited with code ${code}.`);
      stop(code ?? 1);
    }
  });
  return child;
}

function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  const remaining = new Set(children.filter((child) => child.exitCode === null));
  for (const child of children) {
    child.once("exit", () => {
      remaining.delete(child);
      if (remaining.size === 0) process.exit(code);
    });
    try {
      if (process.platform === "win32") child.kill("SIGTERM");
      else process.kill(-child.pid, "SIGTERM");
    } catch {}
  }
  setTimeout(() => {
    for (const child of remaining) {
      try {
        if (process.platform === "win32") child.kill("SIGKILL");
        else process.kill(-child.pid, "SIGKILL");
      } catch {}
    }
  }, 1200);
  setTimeout(() => process.exit(code), 1600);
}

process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));

const marketPort = await choosePort(8787);
const frontendPort = await choosePort(3000);
const marketUrl = `http://127.0.0.1:${marketPort}`;

run(".venv/bin/python", ["-m", "uvicorn", "apps.market_service.service:app", "--host", "127.0.0.1", "--port", String(marketPort)]);
run("npm", ["run", "dev", "-w", "apps/web", "--", "--hostname", "127.0.0.1", "--port", String(frontendPort)], {
  NEXT_PUBLIC_MARKET_SERVICE_URL: "/market-api",
  NEXT_PUBLIC_MARKET_SERVICE_WS_URL: marketUrl.replace(/^http/, "ws"),
  MARKET_SERVICE_INTERNAL_URL: marketUrl
});

console.log("\nTrading Assistant started");
console.log(`Frontend: http://localhost:${frontendPort}`);
console.log(`Market service: ${marketUrl}`);
console.log("Mode: Replay");
console.log("Dataset: registered local Databento MBO sessions");
console.log("Stop cleanly with Ctrl+C.\n");

await new Promise(() => {});
