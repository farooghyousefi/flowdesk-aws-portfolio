from __future__ import annotations

import asyncio
import bisect
from collections import deque
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any

from apps.connectors.databento.src.dbn_reader import F_LAST, MboEvent, OrderBook, SnapshotStatus, iter_events
from .contracts import book_contract
from .decisions import explanation, risk_decision, setup_decision
from .features import OrderflowFeatures
from .microstructure import MicrostructureFeatures
from .research_context import HistoricalContextIndex
from .signal_engine import SignalEngine
from .strategy_search import FAMILY_LABELS, strategy_setup_decision
from .storage import get_session, get_settings, list_strategy_versions, save_signal_snapshot

ALLOWED_SPEEDS = {"0.25", "0.5", "1", "2", "5", "10", "50", "max"}
GROUPS_PER_FRAME = {"0.25": 1, "0.5": 1, "1": 2, "2": 4, "5": 10, "10": 20, "50": 100, "max": 1000}


class ReplayEngine:
    def __init__(self) -> None:
        self._lock = RLock()
        self.session: dict[str, Any] | None = None
        self.events: list[MboEvent] = []
        self.group_ends: list[int] = []
        self.cursor = 0
        self.group_cursor = 0
        self.prime_group = 0
        self.book = OrderBook()
        self.features = OrderflowFeatures()
        self.microstructure = MicrostructureFeatures()
        self.context_index: HistoricalContextIndex | None = None
        self.heatmap: deque[dict[str, Any]] = deque(maxlen=1800)
        self.last_heatmap_ns = 0
        self.playing = False
        self.speed = "1"
        self.revision = 0
        self.loading_session_id: str | None = None
        self.load_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self.blind_mode = "practice"
        self.blind_plan_id: str | None = None
        self.blind_run_id: str | None = None
        self.blind_trade_plans: set[int] = set()
        self.pending_trade_plan = False
        self.signal_engine = SignalEngine(persist=self._persist_signal)
        self._strategy_cache: dict[str, Any] | None = None
        self._strategy_cache_at = 0.0

    @staticmethod
    def _persist_signal(payload: dict[str, Any]) -> None:
        try:
            save_signal_snapshot(payload)
        except Exception:
            # Direct engine tests can use synthetic run IDs without a persisted FK.
            return

    def load(self, session_id: str) -> dict[str, Any]:
        session = get_session(session_id)
        if session is None:
            raise ValueError("Unknown session.")
        path = Path(session["file_path"])

        # Parsing a large DBN file can take longer than the frontend proxy request.
        # Keep the current replay state readable while staging the new session, then
        # swap it atomically under the lock. The HTTP layer can call prepare_load()
        # first so the UI sees the loading state before the worker thread starts.
        with self._lock:
            if self.loading_session_id is None:
                self.playing = False
                self.loading_session_id = session_id
                self.load_error = None
                self.revision += 1
            elif self.loading_session_id != session_id:
                raise ValueError("Another replay session is still loading.")

        try:
            start_at = session.get("start_at")
            end_at = session.get("end_at")
            context_index = (
                HistoricalContextIndex.load(str(start_at), str(end_at))
                if start_at and end_at
                else HistoricalContextIndex.empty()
            )
            events = list(iter_events(path))
            group_ends = [index + 1 for index, event in enumerate(events) if event.flags & F_LAST]
            if not group_ends or group_ends[-1] != len(events):
                group_ends.append(len(events))
            prime_group = self._find_prime_group_for(session, events, group_ends)
        except Exception as exc:
            with self._lock:
                self.loading_session_id = None
                self.load_error = str(exc)
                self.revision += 1
            raise

        with self._lock:
            self.session = session
            self.events = events
            self.group_ends = group_ends
            self.prime_group = prime_group
            self.context_index = context_index
            self.blind_trade_plans.clear()
            self.pending_trade_plan = False
            self._reset_state()
            self._advance_to_group(self.prime_group)
            self.loading_session_id = None
            self.load_error = None
            self.revision += 1
            return self.state()

    def prepare_load(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if self.loading_session_id is not None:
                if self.loading_session_id == session_id:
                    return self.state()
                raise ValueError("Another replay session is still loading.")
            self.playing = False
            self.loading_session_id = session_id
            self.load_error = None
            self.revision += 1
            return self.state()

    @staticmethod
    def _find_prime_group_for(session: dict[str, Any], events: list[MboEvent], group_ends: list[int]) -> int:
        if session["completeness"] == "partial":
            for index, end in enumerate(group_ends, start=1):
                start = group_ends[index - 2] if index > 1 else 0
                if any(event.action == "T" for event in events[start:end]):
                    return index
            return 1 if group_ends else 0

        probe = OrderBook()
        for index, end in enumerate(group_ends, start=1):
            start = group_ends[index - 2] if index > 1 else 0
            for event in events[start:end]:
                probe.apply(event)
            if probe.snapshot_status == SnapshotStatus.POST_SNAPSHOT:
                return index
        return 1 if group_ends else 0

    def refresh(self) -> None:
        """Publish settings and journal-derived state without moving replay time."""
        with self._lock:
            self.revision += 1

    def _active_strategy(self) -> dict[str, Any] | None:
        now = monotonic()
        if now - self._strategy_cache_at >= 2:
            strategies = list_strategy_versions()
            self._strategy_cache = next((item for item in strategies if item["status"] == "ACTIVE"), None)
            if self._strategy_cache is None:
                self._strategy_cache = next((item for item in strategies if item["status"] == "PAPER_ACTIVE"), None)
            self._strategy_cache_at = now
        return self._strategy_cache

    def configure_blind(self, mode: str, plan_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        if mode not in {"practice", "pilot", "locked"}:
            raise ValueError("Unsupported blind replay mode.")
        with self._lock:
            self.playing = False
            self.blind_mode = mode
            self.blind_plan_id = plan_id
            self.blind_run_id = run_id
            self.blind_trade_plans.clear()
            self.pending_trade_plan = False
            self.revision += 1
            return self.state()

    def exit_blind(self) -> dict[str, Any]:
        with self._lock:
            self.playing = False
            self.blind_mode = "practice"
            self.blind_plan_id = None
            self.blind_run_id = None
            self.blind_trade_plans.clear()
            self.pending_trade_plan = False
            self.revision += 1
            return self.state()

    def record_trade_plan(self) -> None:
        with self._lock:
            self.blind_trade_plans.add(self.group_cursor)
            self.pending_trade_plan = False
            self.revision += 1

    def _guard_continuation(self) -> None:
        if self.blind_mode != "locked" or not self.blind_run_id:
            return
        decision = self.state().get("decision", {})
        if decision.get("state") == "trade_ready" and self.group_cursor not in self.blind_trade_plans:
            self.playing = False
            self.pending_trade_plan = True
            raise ValueError("Locked replay requires entry, stop, and target before continuation.")

    def _reset_state(self) -> None:
        self.cursor = 0
        self.group_cursor = 0
        self.book = OrderBook()
        settings = get_settings()["orderflow"]
        self.features = OrderflowFeatures(
            large_trade_threshold=int(settings["largeTradeThreshold"]),
            imbalance_ratio=float(settings["imbalanceRatio"]),
            absorption_window_seconds=int(settings.get("absorptionWindowSeconds", 3)),
            absorption_minimum_observations=int(settings.get("absorptionMinimumObservations", 3)),
            absorption_minimum_elapsed_ms=int(settings.get("absorptionMinimumElapsedMs", 500)),
            absorption_minimum_aggressive_volume=int(settings.get("absorptionMinimumAggressiveVolume", 20)),
            absorption_candidate_limit=int(settings.get("absorptionCandidateLimit", 5)),
            replenishment_threshold=int(settings.get("replenishmentThreshold", 3)),
        )
        self.microstructure = MicrostructureFeatures(
            large_trade_size=int(settings["largeTradeThreshold"]),
        )
        self.heatmap.clear()
        self.last_heatmap_ns = 0
        self.signal_engine.reset()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.playing = False
            self._reset_state()
            self._advance_to_group(self.prime_group)
            self.revision += 1
            return self.state()

    def _apply_event(self, event: MboEvent) -> None:
        before_order = self.book.orders.get(event.order_id)
        self.features.observe(event, before_order=before_order)
        complete_group = self.book.apply(event)
        self.microstructure.observe(event, book=self.book, before_order=before_order)
        if complete_group and event.ts_event - self.last_heatmap_ns >= 250_000_000:
            snapshot = self.book.snapshot(10)
            self.heatmap.append({
                "timestampNs": str(event.ts_event),
                "bids": [{"price": level.price / 1_000_000_000, "size": level.total_size} for level in snapshot.bids],
                "asks": [{"price": level.price / 1_000_000_000, "size": level.total_size} for level in snapshot.asks],
            })
            self.last_heatmap_ns = event.ts_event

    def _advance_to_group(self, target_group: int) -> None:
        target_group = min(max(target_group, 0), len(self.group_ends))
        while self.group_cursor < target_group:
            end = self.group_ends[self.group_cursor]
            for event in self.events[self.cursor:end]:
                self._apply_event(event)
            self.cursor = end
            self.group_cursor += 1

    def step_group(self, count: int = 1) -> dict[str, Any]:
        with self._lock:
            self.playing = False
            self._guard_continuation()
            self._advance_to_group(self.group_cursor + max(1, count))
            self.revision += 1
            return self.state()

    def step_trade(self) -> dict[str, Any]:
        with self._lock:
            self.playing = False
            self._guard_continuation()
            start_trade_count = self.features.trade_count
            while self.group_cursor < len(self.group_ends) and self.features.trade_count == start_trade_count:
                self._advance_to_group(self.group_cursor + 1)
            self.revision += 1
            return self.state()

    def seek(self, *, progress: float | None = None, timestamp_ns: int | None = None) -> dict[str, Any]:
        with self._lock:
            self.playing = False
            if timestamp_ns is not None:
                timestamps = [self.events[end - 1].ts_event for end in self.group_ends]
                target = bisect.bisect_right(timestamps, timestamp_ns)
            else:
                ratio = min(max(float(progress or 0), 0), 1)
                target = round(ratio * len(self.group_ends))
            if self.blind_run_id and self.blind_mode in {"pilot", "locked"} and target > self.group_cursor:
                raise ValueError("Future seek is disabled in blind replay.")
            target = max(target, self.prime_group if self.session and self.session["completeness"] == "complete" else 1)
            self._reset_state()
            self._advance_to_group(target)
            self.revision += 1
            return self.state()

    def jump(self, kind: str) -> dict[str, Any]:
        if self.blind_run_id and self.blind_mode in {"pilot", "locked"}:
            raise ValueError("Future jumps are disabled in blind replay.")
        if kind == "first_trade":
            with self._lock:
                self._reset_state()
                while self.group_cursor < len(self.group_ends) and self.features.trade_count == 0:
                    self._advance_to_group(self.group_cursor + 1)
                self.revision += 1
                return self.state()
        if kind == "high_volume":
            threshold = int(get_settings()["orderflow"]["largeTradeThreshold"])
            for event in self.events[self.cursor:]:
                if event.action == "T" and event.size >= threshold:
                    return self.seek(timestamp_ns=event.ts_event)
        return self.state()

    def jump_to_candidate(self, timestamp_ns: int) -> dict[str, Any]:
        """Move to one audited setup candidate without exposing any later event."""
        with self._lock:
            if not self.events:
                raise ValueError("Load a replay session before jumping to a candidate.")
            timestamps = [self.events[end - 1].ts_event for end in self.group_ends]
            target = bisect.bisect_right(timestamps, timestamp_ns)
            target = max(target, self.prime_group if self.session and self.session["completeness"] == "complete" else 1)
            self.playing = False
            self._reset_state()
            self._advance_to_group(target)
            self.blind_trade_plans.clear()
            self.pending_trade_plan = False
            self.revision += 1
            return self.state()

    def set_speed(self, speed: str) -> dict[str, Any]:
        if speed not in ALLOWED_SPEEDS:
            raise ValueError("Unsupported replay speed.")
        with self._lock:
            self.speed = speed
            self.revision += 1
            return self.state()

    def play(self) -> dict[str, Any]:
        with self._lock:
            if self.loading_session_id is not None:
                raise ValueError("Wait until the selected replay session has finished loading.")
            self._guard_continuation()
            self.playing = True
            self.revision += 1
            return self.state()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            self.playing = False
            self.revision += 1
            return self.state()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            with self._lock:
                if not self.playing or not self.events:
                    continue
                try:
                    self._guard_continuation()
                except ValueError:
                    self.revision += 1
                    continue
                self._advance_to_group(self.group_cursor + GROUPS_PER_FRAME[self.speed])
                if self.group_cursor >= len(self.group_ends):
                    self.playing = False
                self.revision += 1

    def state(self) -> dict[str, Any]:
        with self._lock:
            if not self.session or not self.events:
                return {
                    "version": 1, "loaded": False, "playing": False, "revision": self.revision,
                    "loading": self.loading_session_id is not None,
                    "loadingSessionId": self.loading_session_id,
                    "loadError": self.load_error,
                }
            current = self.events[max(self.cursor - 1, 0)]
            snapshot = self.book.snapshot(10)
            complete = self.session["completeness"] == "complete" and self.book.is_snapshot_ready
            feature_contract = self.features.contract(data_complete=complete)
            microstructure = self.microstructure.contract(self.book)
            feature_contract["microstructure"] = microstructure
            feature_contract["topOfBookLiquidityContracts"] = microstructure["orderBook"]["topOfBookLiquidityContracts"]
            feature_contract["externalContext"] = self.context_index.snapshot(current.ts_event) if self.context_index else {
                "calendarCoverage": "missing", "newsCoverage": "missing", "eventRisk": "clear",
                "newsRisk": "clear", "gate": "clear", "gateReasons": [], "pointInTimeSafe": True,
            }
            risk = risk_decision(timestamp=current.timestamp)
            active_strategy = self._active_strategy()
            if active_strategy and active_strategy.get("config", {}).get("family") in FAMILY_LABELS:
                decision = strategy_setup_decision(
                    timestamp=current.timestamp,
                    completeness="complete" if complete else "partial",
                    features=feature_contract,
                    risk=risk,
                    strategy=active_strategy,
                )
            else:
                decision = setup_decision(
                    timestamp=current.timestamp, completeness="complete" if complete else "partial",
                    features=feature_contract, risk=risk,
                )
            strategy_status = (
                "VALIDATED" if active_strategy and active_strategy["status"] == "ACTIVE"
                else "PAPER_ONLY" if active_strategy and active_strategy["status"] == "PAPER_ACTIVE"
                else "RESEARCH_ONLY"
            )
            signal = self.signal_engine.update(
                timestamp=current.timestamp, timestamp_ns=current.ts_event,
                setup_decision=decision, risk=risk, features=feature_contract,
                completeness="complete" if complete else "partial",
                session_id=self.session["id"], run_id=self.blind_run_id,
                strategy_status=strategy_status,
                strategy_version=f"{active_strategy['version']}:{active_strategy['strategy_hash'][:12]}" if active_strategy else "mes-retest-research-v1",
            )
            progress = self.group_cursor / max(len(self.group_ends), 1)
            return {
                "version": 1, "loaded": True, "mode": "replay", "playing": self.playing, "speed": self.speed,
                "revision": self.revision, "session": self.session,
                "loading": self.loading_session_id is not None,
                "loadingSessionId": self.loading_session_id,
                "loadError": self.load_error,
                "eventCursor": self.cursor,
                "eventCount": len(self.events), "eventGroupCursor": self.group_cursor,
                "eventGroupCount": len(self.group_ends), "progress": round(progress, 6),
                "timestamp": current.timestamp, "timestampNs": str(current.ts_event),
                "book": book_contract(snapshot, timestamp_ns=current.ts_event, instrument_id=current.instrument_id, complete=complete),
                "features": feature_contract, "heatmap": list(self.heatmap), "decision": decision, "signal": signal,
                "risk": risk, "explanation": explanation(decision, risk),
                "liveStatus": "Live data unavailable – Replay mode active",
                "manualExecutionOnly": True,
                "blind": {
                    "mode": self.blind_mode, "planId": self.blind_plan_id, "runId": self.blind_run_id,
                    "status": "ACTIVE" if self.blind_run_id else "NOT_STARTED",
                    "futureSeekAllowed": not self.blind_run_id or self.blind_mode == "practice",
                    "settingsLocked": bool(self.blind_run_id and self.blind_mode == "locked"),
                    "pendingTradePlan": self.pending_trade_plan,
                },
                "applicationLock": {
                    "locked": bool(self.blind_run_id and self.blind_mode == "locked"),
                    "reason": "active_locked_run" if self.blind_run_id and self.blind_mode == "locked" else "none",
                    "protocolId": self.blind_plan_id,
                    "runId": self.blind_run_id,
                    "sessionId": self.session["id"] if self.blind_run_id else None,
                },
            }

    def scan_candidates(self, session_id: str, *, max_rows: int = 500) -> dict[str, Any]:
        self.load(session_id)
        counts = {"trade_ready": 0, "wait": 0, "blocked": 0}
        rows: list[dict[str, Any]] = []
        previous: tuple[str, str | None, tuple[str, ...]] | None = None
        while self.group_cursor < len(self.group_ends):
            state = self.step_group()
            decision = state["decision"]
            decision_state = str(decision["state"])
            counts[decision_state] += 1
            signature = (
                decision_state, decision.get("direction"),
                tuple(decision.get("reasonCodes", [])),
            )
            if len(rows) < max_rows and (signature != previous or decision_state == "trade_ready"):
                rows.append({
                    "timestamp": state["timestamp"], "timestampNs": state["timestampNs"],
                    "decision": decision_state, "direction": decision.get("direction"),
                    "confidence": decision["confidence"], "dataQuality": decision["dataReliability"],
                    "reasons": decision.get("reasonCodes", []),
                })
            previous = signature
        return {"sessionId": session_id, "counts": counts, "candidates": rows, "engine": "ReplayEngine"}
