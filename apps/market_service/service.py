from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .backtest_protocol import (
    assign_session_to_plan,
    clone_session_assignment_into_practice,
    conservative_report,
    create_plan,
    exit_locked_run,
    finish_trade,
    jump_to_candidate,
    protocol_status,
    record_trade_plan,
    scan_session,
    start_blind_session,
)
from .authorization import (
    AuthorizationError,
    authorize_download,
    cancel_authorization_job,
    execution_mode as batch_execution_mode,
    get_authorization,
    list_download_jobs,
    mark_queue_failed,
    prepare_queue_retry,
    reconcile_existing_jobs,
    submit_authorization,
)
from .decisions import backtest_summary
from .importer import import_file
from .planner import (
    download_ready_job,
    estimate_plan,
    estimate_public,
    optimize_plan,
    preview_request_plan,
    refresh_jobs,
    review_purchase,
    submit_purchase,
)
from .range_planner import (
    authorize_range_plan,
    get_range_plan_public,
    list_range_plans_public,
    preview_range_plan,
    ready_range_job_ids,
)
from .planner_jobs import (
    cancel_estimate_job,
    create_estimate_job,
    expire_estimate_jobs,
    public_job,
    retry_estimate_job,
    run_estimate_job,
)
from .market_providers import LiveProvider
from .replay import ReplayEngine
from .research import (
    cancel_research_job,
    create_research_job,
    pause_research_job,
    promote_strategy,
    reject_strategy,
    research_status,
    resume_research_job,
    rollback_strategy,
    run_research_job,
)
from .storage import (
    derive_application_lock_state,
    delete_journal,
    exit_active_backtest_run,
    get_application_state,
    get_planner_state,
    get_estimate_job,
    get_session,
    get_settings,
    import_journal,
    journal_backup,
    journal_csv,
    list_data_estimates,
    list_dataset_jobs,
    list_estimate_jobs,
    list_journal,
    list_research_jobs,
    list_sessions,
    migrate,
    recoverable_estimate_jobs,
    append_audit,
    save_journal,
    get_research_job,
    session_library,
    set_session_split,
    tracked_costs,
    update_settings,
)

engine = ReplayEngine()
live_provider = LiveProvider()
estimate_tasks: dict[str, asyncio.Task[None]] = {}
research_tasks: dict[str, asyncio.Task[None]] = {}
authorization_tasks: dict[str, asyncio.Task[None]] = {}
range_download_tasks: dict[str, asyncio.Task[None]] = {}
authorization_semaphore = asyncio.Semaphore(1)


async def _execute_estimate_job(job_id: str) -> None:
    try:
        await asyncio.to_thread(run_estimate_job, job_id)
    finally:
        estimate_tasks.pop(job_id, None)


def _schedule_estimate_job(job_id: str) -> None:
    if job_id in estimate_tasks and not estimate_tasks[job_id].done():
        return
    estimate_tasks[job_id] = asyncio.create_task(_execute_estimate_job(job_id))


async def _execute_research_job(job_id: str) -> None:
    try:
        await asyncio.to_thread(run_research_job, job_id)
    finally:
        research_tasks.pop(job_id, None)


def _schedule_research_job(job_id: str) -> None:
    if job_id in research_tasks and not research_tasks[job_id].done():
        return
    research_tasks[job_id] = asyncio.create_task(_execute_research_job(job_id))


async def _execute_authorization(authorization_id: str) -> None:
    try:
        async with authorization_semaphore:
            await asyncio.to_thread(submit_authorization, authorization_id)
    except AuthorizationError:
        pass
    finally:
        authorization_tasks.pop(authorization_id, None)


def _schedule_authorization(authorization_id: str) -> None:
    if authorization_id in authorization_tasks and not authorization_tasks[authorization_id].done():
        return
    authorization_tasks[authorization_id] = asyncio.create_task(_execute_authorization(authorization_id))


async def _execute_range_download(plan_id: str) -> None:
    try:
        for job_id in ready_range_job_ids(plan_id):
            await asyncio.to_thread(download_ready_job, job_id)
    finally:
        range_download_tasks.pop(plan_id, None)


def _schedule_range_download(plan_id: str) -> int:
    if plan_id in range_download_tasks and not range_download_tasks[plan_id].done():
        return len(ready_range_job_ids(plan_id))
    ready = ready_range_job_ids(plan_id)
    if ready:
        range_download_tasks[plan_id] = asyncio.create_task(_execute_range_download(plan_id))
    return len(ready)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    migrate()
    reconcile_existing_jobs()
    expire_estimate_jobs()
    interrupted = exit_active_backtest_run(status="INTERRUPTED")
    if interrupted:
        engine.exit_blind()
    sessions = list_sessions()
    complete = next((session for session in sessions if session["completeness"] == "complete"), None)
    if complete:
        await asyncio.to_thread(engine.load, complete["id"])
    task = asyncio.create_task(engine.run())
    for estimate_job in recoverable_estimate_jobs():
        _schedule_estimate_job(estimate_job["id"])
    for research_job in list_research_jobs(500):
        if research_job["status"] in {"QUEUED", "RUNNING"}:
            _schedule_research_job(research_job["id"])
    try:
        yield
    finally:
        for estimate_task in tuple(estimate_tasks.values()):
            estimate_task.cancel()
        for research_task in tuple(research_tasks.values()):
            research_task.cancel()
        for authorization_task in tuple(authorization_tasks.values()):
            authorization_task.cancel()
        for range_download_task in tuple(range_download_tasks.values()):
            range_download_task.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Flowdesk Local Market Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["content-type"],
)


class ReplayLoad(BaseModel):
    sessionId: str


class ReplaySeek(BaseModel):
    progress: float | None = Field(default=None, ge=0, le=1)
    timestampNs: int | None = Field(default=None, ge=0)


class ReplaySpeed(BaseModel):
    speed: str


class ReplayStep(BaseModel):
    kind: str = "event_group"


class ReplayJump(BaseModel):
    kind: str


class ImportRequest(BaseModel):
    file: str


class JournalImport(BaseModel):
    entries: list[dict[str, Any]]


class PlannerRequest(BaseModel):
    market: str = "MES"
    dataset: str = "GLBX.MDP3"
    symbol: str = "MES.v.0"
    date: str
    timezone: str = "Europe/Berlin"
    replayStart: str = "15:00"
    replayEnd: str = "16:30"
    contextMinutes: int = Field(default=30, ge=0, le=1440)
    days: int = Field(default=1, ge=1, le=1)


class RangePlannerRequest(BaseModel):
    market: str = "MES"
    dataset: str = "GLBX.MDP3"
    symbol: str = "MES.v.0"
    startDate: str
    endDate: str
    timezone: str = "Europe/Berlin"
    replayStart: str = "00:00"
    replayEnd: str = "22:00"
    contextMinutes: int = Field(default=0, ge=0, le=1440)
    budgetUsd: float = Field(default=125, gt=0, le=500)
    includeWeekends: bool = False


class RangeEstimateJobCreate(RangePlannerRequest):
    kind: str = "range"


class RangeDownloadAuthorize(BaseModel):
    rangePlanId: str
    acceptedTerms: bool
    confirmationPhrase: str
    displayedAuthorizationAmount: str
    idempotencyKey: str


class PurchaseSubmit(BaseModel):
    estimateId: str
    acknowledged: bool = False
    confirmation: str = ""


class DownloadAuthorize(BaseModel):
    estimateId: str
    fingerprint: str
    mode: str
    acceptedTerms: bool
    confirmationPhrase: str
    displayedAuthorizationAmount: str
    idempotencyKey: str


class EstimateJobCreate(PlannerRequest):
    kind: str = "estimate"


class SessionSplitUpdate(BaseModel):
    splitName: str
    reason: str = ""
    lock: bool = False


class BacktestPlanCreate(BaseModel):
    strategy: str = "MES Pullback / Retest"
    instrument: str = "MES"
    sessionIds: list[str]
    mode: str = "practice"
    fill: dict[str, Any] = Field(default_factory=dict)
    startingBalance: float = 50_000
    riskPerTrade: float = 75
    maximumTradesPerDay: int = 3
    requireFullL3: bool = True


class PlanSessionAssignment(BaseModel):
    sessionId: str


class PracticeSessionClone(BaseModel):
    sessionId: str
    uiPracticeOnly: bool = False


class CandidateScan(BaseModel):
    sessionId: str
    planId: str | None = None


class CandidateJump(BaseModel):
    candidateId: int


class BlindStart(BaseModel):
    planId: str
    sessionId: str


class BlindTradePlan(BaseModel):
    direction: str
    entry: float
    stop: float
    targets: list[float]
    contracts: int = Field(default=1, ge=1)


class BlindTradeClose(BaseModel):
    exitPrice: float
    exitReason: str = "manual"
    mae: float = 0
    mfe: float = 0
    holdingSeconds: float = Field(default=0, ge=0)


class ResearchJobCreate(BaseModel):
    sessionId: str
    name: str | None = None
    strategy: str = "MES Orderflow Baseline"
    fillMode: str = "realistic"
    seed: int = 7
    deltaThreshold: int = Field(default=20, ge=1)
    stopTicks: int = Field(default=8, ge=1)
    targetTicks: int = Field(default=16, ge=1)
    candidateCooldownSeconds: int = Field(default=30, ge=1)


def _safe(operation: Any) -> Any:
    try:
        return operation()
    except AuthorizationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public()) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, Any]:
    state = engine.state()
    return {
        "status": "ok", "service": "market", "version": 1, "binding": "127.0.0.1",
        "mode": "replay", "sessionLoaded": bool(state.get("loaded")),
        "liveEnabled": False, "automaticOrderExecution": False,
        "databentoBatchExecutionMode": batch_execution_mode(),
    }


@app.get("/sessions")
def sessions() -> list[dict[str, Any]]:
    return list_sessions()


@app.get("/sessions/{session_id}")
def session(session_id: str) -> dict[str, Any]:
    result = get_session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return result


@app.post("/data/import")
async def data_import(payload: ImportRequest) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: import_file(payload.file)))


@app.post("/replay/load")
async def replay_load(payload: ReplayLoad) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        active_run = get_application_state().get("activeRun")
        if active_run and active_run["session_id"] != payload.sessionId:
            exit_active_backtest_run(status="EXITED_SESSION_CHANGE")
            engine.exit_blind()
        return engine.load(payload.sessionId)
    return await asyncio.to_thread(lambda: _safe(load))


@app.post("/replay/play")
def replay_play() -> dict[str, Any]:
    return _safe(engine.play)


@app.post("/replay/pause")
def replay_pause() -> dict[str, Any]:
    return engine.pause()


@app.post("/replay/reset")
def replay_reset() -> dict[str, Any]:
    return engine.reset()


@app.post("/replay/seek")
def replay_seek(payload: ReplaySeek) -> dict[str, Any]:
    return _safe(lambda: engine.seek(progress=payload.progress, timestamp_ns=payload.timestampNs))


@app.post("/replay/speed")
def replay_speed(payload: ReplaySpeed) -> dict[str, Any]:
    return _safe(lambda: engine.set_speed(payload.speed))


@app.post("/replay/step")
def replay_step(payload: ReplayStep) -> dict[str, Any]:
    return _safe(lambda: engine.step_trade() if payload.kind == "trade" else engine.step_group())


@app.post("/replay/jump")
def replay_jump(payload: ReplayJump) -> dict[str, Any]:
    return _safe(lambda: engine.jump(payload.kind))


@app.get("/replay/state")
def replay_state() -> dict[str, Any]:
    return engine.state()


@app.websocket("/replay/stream")
async def replay_stream(websocket: WebSocket) -> None:
    client_host = websocket.client.host if websocket.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    last_revision = -1
    try:
        while True:
            state = engine.state()
            revision = int(state.get("revision", 0))
            if revision != last_revision:
                await websocket.send_json(state)
                last_revision = revision
            await asyncio.sleep(0.05)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return


@app.get("/settings")
def settings() -> dict[str, Any]:
    return get_settings()


@app.put("/settings")
def settings_update(payload: dict[str, Any]) -> dict[str, Any]:
    if derive_application_lock_state()["locked"]:
        raise HTTPException(status_code=423, detail="Settings are locked for the active backtest plan.")
    before = get_settings()
    result = update_settings(payload)
    changed_sections = [section for section in payload if before.get(section) != result.get(section)]
    if changed_sections:
        append_audit("SETTINGS_CHANGED", {"sections": changed_sections, "secretValuesLogged": False})
    if "risk" in changed_sections:
        append_audit("RISK_LIMIT_CHANGED", {"fields": sorted(payload.get("risk", {}).keys()), "valuesLogged": False})
    engine.refresh()
    return result


@app.get("/journal")
def journal() -> list[dict[str, Any]]:
    return list_journal()


@app.post("/journal")
def journal_create(payload: dict[str, Any]) -> dict[str, Any]:
    result = save_journal(payload, str(uuid.uuid4()))
    engine.refresh()
    return result


@app.put("/journal/{entry_id}")
def journal_update(entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = save_journal(payload, entry_id)
    engine.refresh()
    return result


@app.delete("/journal/{entry_id}")
def journal_delete(entry_id: str) -> dict[str, bool]:
    deleted = delete_journal(entry_id)
    engine.refresh()
    return {"deleted": deleted}


@app.post("/journal/import")
def journal_import(payload: JournalImport) -> dict[str, int]:
    imported = import_journal(payload.entries)
    engine.refresh()
    return {"imported": imported}


@app.get("/journal/export.csv", response_class=PlainTextResponse)
def journal_export_csv() -> PlainTextResponse:
    return PlainTextResponse(journal_csv(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=flowdesk-journal.csv"})


@app.get("/journal/backup.json", response_class=PlainTextResponse)
def journal_export_json() -> PlainTextResponse:
    return PlainTextResponse(journal_backup(), media_type="application/json", headers={"Content-Disposition": "attachment; filename=flowdesk-journal.json"})


@app.get("/backtest")
def backtest() -> dict[str, Any]:
    return backtest_summary()


@app.post("/data-planner/range/preview")
def data_planner_range_preview(payload: RangePlannerRequest) -> dict[str, Any]:
    return _safe(lambda: preview_range_plan(payload.model_dump()))


@app.post("/data-planner/range/estimate-jobs")
async def data_planner_range_estimate_job_create(payload: RangeEstimateJobCreate) -> dict[str, Any]:
    request = payload.model_dump(exclude={"kind"})
    job = _safe(lambda: create_estimate_job(request, job_kind="range"))
    if job["status"] in {"PENDING", "RUNNING"}:
        _schedule_estimate_job(job["id"])
    return job


@app.get("/data-planner/range-plans/{plan_id}")
def data_planner_range_plan(plan_id: str) -> dict[str, Any]:
    return _safe(lambda: get_range_plan_public(plan_id))


@app.post("/data-planner/range-plans/{plan_id}/authorize")
async def data_planner_range_authorize(plan_id: str, payload: RangeDownloadAuthorize) -> dict[str, Any]:
    if plan_id != payload.rangePlanId:
        raise HTTPException(status_code=400, detail={"code": "RANGE_PLAN_ID_MISMATCH", "message": "Path and payload range plan IDs differ.", "nextAction": "Reload the range plan."})
    result = await asyncio.to_thread(lambda: _safe(lambda: authorize_range_plan(payload.model_dump())))
    if result["executionMode"] == "live":
        for authorization_id in result["authorizationIds"]:
            _schedule_authorization(authorization_id)
    return result


@app.post("/data-planner/range-plans/{plan_id}/download-ready")
async def data_planner_range_download_ready(plan_id: str) -> dict[str, Any]:
    ready = _safe(lambda: _schedule_range_download(plan_id))
    return {
        "rangePlan": _safe(lambda: get_range_plan_public(plan_id)),
        "scheduledReadyJobs": ready,
        "backgroundDownloadStarted": ready > 0,
    }


@app.post("/data-planner/estimate")
async def data_planner_estimate(payload: PlannerRequest) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: estimate_plan(payload.model_dump())))


@app.post("/data-planner/estimate-jobs")
async def data_planner_estimate_job_create(payload: EstimateJobCreate) -> dict[str, Any]:
    request = payload.model_dump(exclude={"kind"})
    job = _safe(lambda: create_estimate_job(request, job_kind=payload.kind))
    if job["status"] in {"PENDING", "RUNNING"}:
        _schedule_estimate_job(job["id"])
    return job


@app.get("/data-planner/estimate-jobs/{job_id}")
def data_planner_estimate_job(job_id: str) -> dict[str, Any]:
    expire_estimate_jobs()
    job = get_estimate_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Estimate job not found.")
    return public_job(job)


@app.post("/data-planner/estimate-jobs/{job_id}/retry")
async def data_planner_estimate_job_retry(job_id: str) -> dict[str, Any]:
    job = _safe(lambda: retry_estimate_job(job_id))
    _schedule_estimate_job(job["id"])
    return job


@app.post("/data-planner/estimate-jobs/{job_id}/cancel")
def data_planner_estimate_job_cancel(job_id: str) -> dict[str, Any]:
    return _safe(lambda: cancel_estimate_job(job_id))


@app.post("/data-planner/preview")
def data_planner_preview(payload: PlannerRequest) -> dict[str, Any]:
    return _safe(lambda: preview_request_plan(payload.model_dump()))


@app.post("/data-planner/optimize")
async def data_planner_optimize(payload: PlannerRequest) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: optimize_plan(payload.model_dump())))


@app.get("/data-planner/status")
def data_planner_status() -> dict[str, Any]:
    expire_estimate_jobs()
    reconcile_existing_jobs()
    estimates = [estimate_public(item) for item in list_data_estimates()]
    return {
        "costs": tracked_costs(), "estimates": estimates, "jobs": list_download_jobs(),
        "estimateJobs": [public_job(item) for item in list_estimate_jobs()],
        "rangePlans": list_range_plans_public(),
        "sessions": session_library(), "requestPlan": (get_planner_state() or {}).get("requestPlan"),
        "downloadStarted": False,
    }


@app.get("/data-planner/estimates/{estimate_id}/review")
def data_planner_review(estimate_id: str) -> dict[str, Any]:
    return _safe(lambda: review_purchase(estimate_id))


@app.post("/data-planner/submit")
async def data_planner_submit(payload: PurchaseSubmit) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail={
        "code": "LEGACY_SUBMISSION_DISABLED",
        "message": "The synchronous submission endpoint is disabled.",
        "nextAction": "Reload Data Planner and use the authorization dialog.",
    })


@app.post("/data-planner/estimates/{estimate_id}/authorize")
async def data_planner_authorize(estimate_id: str, payload: DownloadAuthorize) -> dict[str, Any]:
    if estimate_id != payload.estimateId:
        raise HTTPException(status_code=400, detail={
            "code": "ESTIMATE_ID_MISMATCH", "message": "Path and payload estimate IDs differ.",
            "nextAction": "Reload the purchase review.",
        })
    result = await asyncio.to_thread(lambda: _safe(lambda: authorize_download(payload.model_dump())))
    authorization = result["authorization"]
    if authorization["executionMode"] == "live" and authorization["state"] == "AUTHORIZED" and not result["idempotentReplay"]:
        try:
            _schedule_authorization(authorization["id"])
        except Exception:
            result = mark_queue_failed(authorization["id"], "The local background queue is unavailable.")
    return result


@app.get("/data-planner/authorizations/{authorization_id}")
def data_planner_authorization(authorization_id: str) -> dict[str, Any]:
    result = get_authorization(authorization_id=authorization_id)
    if not result:
        raise HTTPException(status_code=404, detail={"code": "AUTHORIZATION_NOT_FOUND", "message": "Authorization not found.", "nextAction": "Reload Data Planner."})
    return result


@app.get("/data-planner/estimates/{estimate_id}/authorization")
def data_planner_estimate_authorization(estimate_id: str) -> dict[str, Any]:
    result = get_authorization(estimate_id=estimate_id)
    if not result:
        raise HTTPException(status_code=404, detail={"code": "AUTHORIZATION_NOT_FOUND", "message": "No authorization exists for this estimate.", "nextAction": "Review the estimate before authorizing."})
    return result


@app.post("/data-planner/jobs/{job_id}/cancel-authorization")
def data_planner_cancel_authorization(job_id: str) -> dict[str, Any]:
    return _safe(lambda: cancel_authorization_job(job_id))


@app.post("/data-planner/authorizations/{authorization_id}/retry")
def data_planner_retry_authorization(authorization_id: str) -> dict[str, Any]:
    result = _safe(lambda: prepare_queue_retry(authorization_id))
    try:
        _schedule_authorization(authorization_id)
    except Exception:
        return mark_queue_failed(authorization_id, "The local background queue is unavailable.")
    return result


@app.post("/data-planner/jobs/refresh")
async def data_planner_jobs_refresh() -> list[dict[str, Any]]:
    return await asyncio.to_thread(lambda: _safe(refresh_jobs))


@app.post("/data-planner/jobs/{job_id}/download")
async def data_planner_job_download(job_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: download_ready_job(job_id)))


@app.get("/session-library")
def sessions_library() -> list[dict[str, Any]]:
    return session_library()


@app.put("/session-library/{session_id}/split")
def session_split_update(session_id: str, payload: SessionSplitUpdate) -> dict[str, Any]:
    return _safe(lambda: set_session_split(session_id, payload.splitName, payload.reason, lock=payload.lock))


@app.post("/backtest/plans")
def backtest_plan_create(payload: BacktestPlanCreate) -> dict[str, Any]:
    def create() -> dict[str, Any]:
        plan = create_plan(payload.model_dump())
        engine.exit_blind()
        return plan
    return _safe(create)


@app.get("/backtest/plans")
def backtest_plans(planId: str | None = None) -> dict[str, Any]:
    return protocol_status(planId)


@app.post("/backtest/plans/{plan_id}/sessions")
def backtest_plan_assign_session(plan_id: str, payload: PlanSessionAssignment) -> dict[str, Any]:
    return _safe(lambda: assign_session_to_plan(plan_id, payload.sessionId))


@app.post("/backtest/plans/{plan_id}/sessions/clone")
def backtest_plan_clone_session(plan_id: str, payload: PracticeSessionClone) -> dict[str, Any]:
    return _safe(lambda: clone_session_assignment_into_practice(
        plan_id, payload.sessionId, ui_practice_only=payload.uiPracticeOnly,
    ))


@app.post("/backtest/runs/exit")
def backtest_run_exit() -> dict[str, Any]:
    return _safe(lambda: exit_locked_run(engine))


@app.post("/backtest/scan")
async def backtest_scan(payload: CandidateScan) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: scan_session(payload.sessionId, payload.planId)))


@app.post("/backtest/candidates/jump")
async def backtest_candidate_jump(payload: CandidateJump) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: jump_to_candidate(engine, payload.candidateId)))


@app.get("/backtest/report")
def backtest_report(planId: str | None = None) -> dict[str, Any]:
    return conservative_report(planId)


@app.post("/blind/start")
async def blind_start(payload: BlindStart) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _safe(lambda: start_blind_session(engine, payload.planId, payload.sessionId)))


@app.post("/blind/trades")
def blind_trade_create(payload: BlindTradePlan) -> dict[str, Any]:
    return _safe(lambda: record_trade_plan(engine, payload.model_dump()))


@app.post("/blind/trades/{trade_id}/close")
def blind_trade_close(trade_id: str, payload: BlindTradeClose) -> dict[str, Any]:
    return _safe(lambda: finish_trade(trade_id, payload.model_dump()))


@app.get("/live/health")
def live_health() -> dict[str, Any]:
    return {**live_provider.status(), "mode": "live", "messageKey": "live.replayOnly", "orderPlacement": False}


@app.get("/research/status")
def research_status_endpoint() -> dict[str, Any]:
    return research_status()


@app.post("/research/jobs")
async def research_job_create(payload: ResearchJobCreate) -> dict[str, Any]:
    created = _safe(lambda: create_research_job(payload.model_dump()))
    _schedule_research_job(created["job"]["id"])
    return created


@app.get("/research/jobs/{job_id}")
def research_job(job_id: str) -> dict[str, Any]:
    job = get_research_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Research job not found.")
    return job


@app.post("/research/jobs/{job_id}/cancel")
def research_job_cancel(job_id: str) -> dict[str, Any]:
    return _safe(lambda: cancel_research_job(job_id))


@app.post("/research/jobs/{job_id}/pause")
def research_job_pause(job_id: str) -> dict[str, Any]:
    return _safe(lambda: pause_research_job(job_id))


@app.post("/research/jobs/{job_id}/resume")
async def research_job_resume(job_id: str) -> dict[str, Any]:
    resumed = _safe(lambda: resume_research_job(job_id))
    _schedule_research_job(job_id)
    return resumed


@app.post("/research/strategies/{strategy_hash}/promote")
def research_strategy_promote(strategy_hash: str) -> dict[str, Any]:
    return _safe(lambda: promote_strategy(strategy_hash))


@app.post("/research/strategies/{strategy_hash}/reject")
def research_strategy_reject(strategy_hash: str) -> dict[str, Any]:
    return _safe(lambda: reject_strategy(strategy_hash))


@app.post("/research/strategies/{strategy_hash}/rollback")
def research_strategy_rollback(strategy_hash: str) -> dict[str, Any]:
    return _safe(lambda: rollback_strategy(strategy_hash))


@app.get("/signals/current")
def current_signal() -> dict[str, Any]:
    state = engine.state()
    return state.get("signal") or {
        "status": "NO_TRADE", "reason": "NO_REPLAY_SESSION", "manualExecutionOnly": True,
        "automaticOrderExecution": False,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("apps.market_service.service:app", host="127.0.0.1", port=8787, reload=False)
