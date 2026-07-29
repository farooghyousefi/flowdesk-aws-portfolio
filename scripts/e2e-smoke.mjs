import { spawn } from "node:child_process";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import net from "node:net";

const children = [];
const authRoot = mkdtempSync(join(tmpdir(), "flowdesk-auth-e2e-"));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const isOpen = (port) => new Promise((resolve) => {
  const socket = net.connect(port, "127.0.0.1");
  socket.once("connect", () => { socket.destroy(); resolve(true); });
  socket.once("error", () => { socket.destroy(); resolve(false); });
});
const choosePort = async (start) => {
  for (let port = start; port < start + 20; port += 1) {
    if (!(await isOpen(port))) return port;
  }
  throw new Error(`No free local port near ${start}.`);
};

function start(command, args, env = {}) {
  const child = spawn(command, args, { cwd: process.cwd(), env: { ...process.env, ...env }, stdio: "ignore", detached: true });
  children.push(child);
  return child;
}

async function stop() {
  const pending = children.filter((child) => child.exitCode === null);
  for (const child of children) {
    try { process.kill(-child.pid, "SIGTERM"); } catch {}
  }
  await delay(600);
  for (const child of pending) {
    if (child.exitCode === null) {
      try { process.kill(-child.pid, "SIGKILL"); } catch {}
    }
  }
  await delay(50);
}

async function waitFor(url, attempts = 120) {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return response;
    } catch {}
    await delay(250);
  }
  throw new Error(`Timed out waiting for ${url}`);
}

try {
  const marketPort = await choosePort(8787);
  const authPort = await choosePort(marketPort + 1);
  const frontendPort = await choosePort(3000);
  const marketUrl = `http://127.0.0.1:${marketPort}`;
  const frontendUrl = `http://127.0.0.1:${frontendPort}`;
  start(".venv/bin/python", ["-m", "uvicorn", "apps.market_service.service:app", "--host", "127.0.0.1", "--port", String(marketPort)]);
  const seed = spawnSync(".venv/bin/python", ["-c", `
import json
from datetime import UTC, datetime, timedelta
from apps.market_service import storage
storage.migrate()
now = datetime.now(UTC)
storage.save_data_estimate({
  "id": "e2e-authorization-estimate", "request_fingerprint": "e2e-authorization-fingerprint",
  "dataset": "GLBX.MDP3", "mode": "full_l3", "schemas_json": json.dumps(["mbo"]),
  "input_symbol": "MES.v.0", "raw_symbol": "MESU6", "instrument_id": 42003239,
  "start_utc": "2026-07-14T00:00:00Z", "end_utc": "2026-07-14T14:30:00Z",
  "replay_start": "2026-07-14T15:00:00+02:00", "replay_end": "2026-07-14T16:30:00+02:00",
  "timezone": "Europe/Berlin", "estimated_cost": 0.766748, "estimated_records": 1000,
  "billable_bytes": 1048576, "unit_price_json": json.dumps({"mbo": 1.8}), "local_reuse": 0,
  "allowed": 1, "confidence": "HIGH", "warnings_json": "[]",
  "metadata_json": json.dumps({
    "rawEstimatedCostUsd": 0.766748, "safetyReserveUsd": 0.076675,
    "maximumAuthorizedUsd": 0.843423, "schemaDetails": [],
    "contract": {"inputSymbol": "MES.v.0", "rawSymbol": "MESU6", "instrumentId": 42003239, "mappingValidFrom": "2026-06-01", "mappingValidTo": "2026-09-01"},
    "conditions": [], "datasetRange": {}, "availableFeatures": ["L3"], "disabledFeatures": [], "suitability": ["Replay"],
    "requestLimitUsd": 1, "dailyLimitUsd": 5, "weeklyLimitUsd": 15, "monthlyLimitUsd": 40,
    "dailyRemainingUsd": 5, "weeklyRemainingUsd": 15, "monthlyRemainingUsd": 40
  }),
  "created_at": now.isoformat().replace("+00:00", "Z"),
  "expires_at": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
  "status": "AWAITING_CONFIRMATION", "job_id": None, "actual_local_size": None, "downloaded_at": None
})
`], { cwd: process.cwd(), env: { ...process.env, FLOWDESK_APP_ROOT: authRoot }, encoding: "utf8" });
  if (seed.status !== 0) throw new Error(`Authorization E2E seed failed: ${seed.stderr}`);
  start(".venv/bin/python", ["-m", "uvicorn", "apps.market_service.service:app", "--host", "127.0.0.1", "--port", String(authPort)], {
    FLOWDESK_APP_ROOT: authRoot,
    DATABENTO_BATCH_EXECUTION_MODE: "dry_run",
  });
  start("npm", ["run", "dev", "-w", "apps/web", "--", "--hostname", "127.0.0.1", "--port", String(frontendPort)], {
    NEXT_PUBLIC_MARKET_SERVICE_URL: "/market-api",
    NEXT_PUBLIC_MARKET_SERVICE_WS_URL: marketUrl.replace(/^http/, "ws"),
    MARKET_SERVICE_INTERNAL_URL: marketUrl,
  });
  const health = await (await waitFor(`${marketUrl}/health`)).json();
  if (health.automaticOrderExecution !== false || health.binding !== "127.0.0.1") throw new Error("Market service safety contract failed.");
  const sessions = await (await fetch(`${marketUrl}/sessions`)).json();
  if (!sessions.some((session) => session.completeness === "complete")) throw new Error("Complete demo session is missing.");
  const state = await (await fetch(`${marketUrl}/replay/state`)).json();
  if (!state.loaded || !state.book || !state.decision || !state.risk) throw new Error("Replay state is incomplete.");
  const planner = await (await fetch(`${marketUrl}/data-planner/status`)).json();
  if (planner.downloadStarted !== false || !Array.isArray(planner.jobs) || planner.sessions.length < 1) throw new Error("Data Planner safety status failed.");
  const preview = await (await fetch(`${marketUrl}/data-planner/preview`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ date: "2026-07-14", timezone: "Europe/Berlin", replayStart: "15:00", replayEnd: "16:30", contextMinutes: 30 }),
  })).json();
  if (preview.requestPlan.replayEndLocal !== "16:30" || preview.requestPlan.replayEndUtc !== "2026-07-14T14:30:00Z" || preview.requestPlan.requestStartUtc !== "2026-07-14T12:30:00Z") {
    throw new Error("Data Planner canonical time contract failed.");
  }
  const protocol = await (await fetch(`${marketUrl}/backtest/plans`)).json();
  if (!Array.isArray(protocol.phases) || protocol.phases.length !== 4) throw new Error("Backtest protocol status failed.");
  if (protocol.applicationLock.locked || state.applicationLock.locked) throw new Error("Inactive or archived plans must not lock the application.");
  const research = await (await fetch(`${marketUrl}/research/status`)).json();
  if (!Array.isArray(research.datasets) || !Array.isArray(research.experiments) || !Array.isArray(research.models)) throw new Error("Research status contract failed.");
  const authUrl = `http://127.0.0.1:${authPort}`;
  await waitFor(`${authUrl}/health`);
  const review = await (await fetch(`${authUrl}/data-planner/estimates/e2e-authorization-estimate/review`)).json();
  if (review.confirmationPhrase !== "DOWNLOAD $0.84" || review.authorizationAmountDisplay !== "0.84" || !review.canSubmit) {
    throw new Error("Authorization review contract failed.");
  }
  const authorizationPayload = {
    estimateId: review.estimate.estimateId,
    fingerprint: review.fingerprint,
    mode: review.estimate.mode,
    acceptedTerms: true,
    confirmationPhrase: review.confirmationPhrase,
    displayedAuthorizationAmount: review.authorizationAmountDisplay,
    idempotencyKey: "e2e-double-submit-key",
  };
  const authorize = () => fetch(`${authUrl}/data-planner/estimates/e2e-authorization-estimate/authorize`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(authorizationPayload),
  }).then(async (response) => {
    const body = await response.json();
    if (!response.ok) throw new Error(`Authorization request failed: ${JSON.stringify(body)}`);
    return body;
  });
  const [authorized, duplicate] = await Promise.all([authorize(), authorize()]);
  if (authorized.authorization.id !== duplicate.authorization.id || authorized.authorization.executionMode !== "dry_run") {
    throw new Error("Authorization idempotency contract failed.");
  }
  const authStatus = await (await fetch(`${authUrl}/data-planner/status`)).json();
  if (authStatus.jobs.length !== 1 || authStatus.jobs[0].remoteJobId !== null || authStatus.jobs[0].actualCostUsd !== null) {
    throw new Error("Dry-run unexpectedly created a remote job or actual charge.");
  }
  await fetch(`${marketUrl}/replay/play`, { method: "POST" });
  await delay(150);
  const paused = await (await fetch(`${marketUrl}/replay/pause`, { method: "POST" })).json();
  if (paused.playing) throw new Error("Replay pause failed.");
  const page = await (await waitFor(`${frontendUrl}/data-planner`)).text();
  if (!page.includes("FLOWDESK") || !page.includes("Replay")) throw new Error("Replay UI did not render server-side.");
  const researchPage = await (await waitFor(`${frontendUrl}/research`)).text();
  if (!researchPage.includes("FLOWDESK") || !researchPage.includes("Research Lab")) throw new Error("Research Lab deep link did not render server-side.");
  console.log("PASS e2e: frontend, replay, atomic Data Planner authorization/idempotency, backtest, and Research Lab contracts are available without a remote order.");
} finally {
  await stop();
  rmSync(authRoot, { recursive: true, force: true });
}
