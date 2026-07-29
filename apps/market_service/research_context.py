from __future__ import annotations

import csv
import hashlib
import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apps.connectors.databento.src.config import REPO_ROOT

from .storage import connect, migrate, utc_now


CONTEXT_ROOT = REPO_ROOT / "data" / "context"
ECONOMIC_CALENDAR_PATH = CONTEXT_ROOT / "economic_calendar.csv"
NEWS_EVENTS_PATH = CONTEXT_ROOT / "news_events.jsonl"
COVERAGE_PATH = CONTEXT_ROOT / "coverage.json"

HIGH_IMPACT_PRE_MINUTES = 10
HIGH_IMPACT_POST_MINUTES = 5
NEWS_BLOCK_SECONDS = 120


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_ns(value: str | None) -> int | None:
    parsed = _parse_timestamp(value)
    return int(parsed.timestamp() * 1_000_000_000) if parsed else None


def _normalized_importance(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    if text in {"3", "high", "hoch", "red"}:
        return "high"
    if text in {"2", "medium", "mittel", "orange"}:
        return "medium"
    if text in {"1", "low", "niedrig", "yellow"}:
        return "low"
    return "unknown"


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "null", "None"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def ensure_context_templates() -> None:
    CONTEXT_ROOT.mkdir(parents=True, exist_ok=True)
    if not ECONOMIC_CALENDAR_PATH.exists():
        ECONOMIC_CALENDAR_PATH.write_text(
            "source_id,scheduled_at,event_name,currency,importance,forecast,actual,previous,published_at,source\n",
            encoding="utf-8",
        )
    if not NEWS_EVENTS_PATH.exists():
        NEWS_EVENTS_PATH.write_text("", encoding="utf-8")
    if not COVERAGE_PATH.exists():
        COVERAGE_PATH.write_text(
            json.dumps(
                {
                    "economicCalendar": {"source": "", "coverageStart": None, "coverageEnd": None},
                    "news": {"source": "", "coverageStart": None, "coverageEnd": None},
                    "note": "Coverage must describe the historical interval actually supplied. Do not infer coverage from file modification time.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coverage_manifest() -> dict[str, Any]:
    ensure_context_templates()
    try:
        payload = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _upsert_source(kind: str, *, source: str, coverage_start: str | None, coverage_end: str | None, row_count: int, file_hash: str) -> None:
    with connect() as database:
        database.execute(
            """INSERT INTO context_sources(kind, source_name, coverage_start, coverage_end, row_count, file_hash, imported_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(kind) DO UPDATE SET source_name=excluded.source_name,
                 coverage_start=excluded.coverage_start, coverage_end=excluded.coverage_end,
                 row_count=excluded.row_count, file_hash=excluded.file_hash, imported_at=excluded.imported_at""",
            (kind, source, coverage_start, coverage_end, row_count, file_hash, utc_now()),
        )


def sync_context_files() -> dict[str, Any]:
    migrate()
    ensure_context_templates()
    manifest = _coverage_manifest()
    calendar_config = manifest.get("economicCalendar") if isinstance(manifest.get("economicCalendar"), dict) else {}
    news_config = manifest.get("news") if isinstance(manifest.get("news"), dict) else {}

    calendar_count = 0
    with ECONOMIC_CALENDAR_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        with connect() as database:
            for row in reader:
                scheduled = _parse_timestamp(row.get("scheduled_at"))
                if not scheduled or not str(row.get("event_name") or "").strip():
                    continue
                scheduled_at = scheduled.isoformat().replace("+00:00", "Z")
                published = _parse_timestamp(row.get("published_at")) or scheduled
                source_id = str(row.get("source_id") or "").strip() or hashlib.sha256(
                    f"{scheduled_at}|{row.get('event_name')}|{row.get('currency')}".encode()
                ).hexdigest()[:24]
                database.execute(
                    """INSERT INTO economic_events(
                       source_id, scheduled_at, event_name, currency, importance, forecast, actual,
                       previous, published_at, source_name, raw_json, imported_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id) DO UPDATE SET scheduled_at=excluded.scheduled_at,
                         event_name=excluded.event_name, currency=excluded.currency,
                         importance=excluded.importance, forecast=excluded.forecast, actual=excluded.actual,
                         previous=excluded.previous, published_at=excluded.published_at,
                         source_name=excluded.source_name, raw_json=excluded.raw_json,
                         imported_at=excluded.imported_at""",
                    (
                        source_id,
                        scheduled_at,
                        str(row.get("event_name") or "").strip(),
                        str(row.get("currency") or "USD").strip().upper(),
                        _normalized_importance(row.get("importance")),
                        _float_or_none(row.get("forecast")),
                        _float_or_none(row.get("actual")),
                        _float_or_none(row.get("previous")),
                        published.isoformat().replace("+00:00", "Z"),
                        str(row.get("source") or calendar_config.get("source") or "local_csv"),
                        json.dumps(row, sort_keys=True),
                        utc_now(),
                    ),
                )
                calendar_count += 1
    _upsert_source(
        "economic_calendar",
        source=str(calendar_config.get("source") or "local_csv"),
        coverage_start=calendar_config.get("coverageStart"),
        coverage_end=calendar_config.get("coverageEnd"),
        row_count=calendar_count,
        file_hash=_file_hash(ECONOMIC_CALENDAR_PATH),
    )

    news_count = 0
    with connect() as database:
        for raw_line in NEWS_EVENTS_PATH.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            published = _parse_timestamp(row.get("published_at"))
            headline = str(row.get("headline") or "").strip()
            if not published or not headline:
                continue
            published_at = published.isoformat().replace("+00:00", "Z")
            source_id = str(row.get("source_id") or "").strip() or hashlib.sha256(
                f"{published_at}|{headline}|{row.get('provider')}".encode()
            ).hexdigest()[:24]
            database.execute(
                """INSERT INTO news_events(
                   source_id, published_at, headline, provider, relevance, sentiment, symbols_json,
                   raw_json, imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET published_at=excluded.published_at,
                     headline=excluded.headline, provider=excluded.provider,
                     relevance=excluded.relevance, sentiment=excluded.sentiment,
                     symbols_json=excluded.symbols_json, raw_json=excluded.raw_json,
                     imported_at=excluded.imported_at""",
                (
                    source_id,
                    published_at,
                    headline,
                    str(row.get("provider") or news_config.get("source") or "local_jsonl"),
                    max(0.0, min(1.0, float(row.get("relevance") or 0))),
                    max(-1.0, min(1.0, float(row.get("sentiment") or 0))),
                    json.dumps(row.get("symbols") or ["MES"]),
                    json.dumps(row, sort_keys=True),
                    utc_now(),
                ),
            )
            news_count += 1
    _upsert_source(
        "news",
        source=str(news_config.get("source") or "local_jsonl"),
        coverage_start=news_config.get("coverageStart"),
        coverage_end=news_config.get("coverageEnd"),
        row_count=news_count,
        file_hash=_file_hash(NEWS_EVENTS_PATH),
    )
    return context_coverage()


def context_coverage() -> dict[str, Any]:
    migrate()
    with connect() as database:
        rows = database.execute("SELECT * FROM context_sources ORDER BY kind").fetchall()
    by_kind = {str(row["kind"]): dict(row) for row in rows}

    def source(kind: str) -> dict[str, Any]:
        row = by_kind.get(kind, {})
        start = row.get("coverage_start")
        end = row.get("coverage_end")
        return {
            "source": row.get("source_name") or "",
            "coverageStart": start,
            "coverageEnd": end,
            "rowCount": int(row.get("row_count") or 0),
            "declaredCoverage": bool(start and end),
            "fileHash": row.get("file_hash") or "",
            "importedAt": row.get("imported_at"),
        }

    return {
        "economicCalendar": source("economic_calendar"),
        "news": source("news"),
        "pointInTimeSafe": True,
        "lookAheadProtection": "Actual values and headlines are unavailable before published_at.",
    }


def _range_covered(source: dict[str, Any], start_at: str, end_at: str) -> bool:
    source_start = _parse_timestamp(source.get("coverageStart"))
    source_end = _parse_timestamp(source.get("coverageEnd"))
    requested_start = _parse_timestamp(start_at)
    requested_end = _parse_timestamp(end_at)
    return bool(source_start and source_end and requested_start and requested_end and source_start <= requested_start and source_end >= requested_end)


@dataclass(frozen=True)
class HistoricalContextIndex:
    events: tuple[dict[str, Any], ...]
    event_times: tuple[int, ...]
    news: tuple[dict[str, Any], ...]
    news_times: tuple[int, ...]
    coverage: dict[str, Any]
    calendar_covered: bool
    news_covered: bool

    @classmethod
    def empty(cls) -> "HistoricalContextIndex":
        """Return a safe no-context index for synthetic/tests or legacy sessions.

        Missing coverage remains explicit so signal gates cannot mistake absent context
        for a verified quiet market.
        """
        return cls(
            events=(),
            event_times=(),
            news=(),
            news_times=(),
            coverage={
                "economicCalendar": {"declaredCoverage": False, "rowCount": 0},
                "news": {"declaredCoverage": False, "rowCount": 0},
                "pointInTimeSafe": True,
            },
            calendar_covered=False,
            news_covered=False,
        )

    @classmethod
    def load(cls, start_at: str, end_at: str) -> "HistoricalContextIndex":
        sync_context_files()
        start = _parse_timestamp(start_at)
        end = _parse_timestamp(end_at)
        if not start or not end:
            raise ValueError("Invalid research interval.")
        expanded_start = (start - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        expanded_end = (end + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        with connect() as database:
            event_rows = database.execute(
                "SELECT * FROM economic_events WHERE scheduled_at BETWEEN ? AND ? ORDER BY scheduled_at",
                (expanded_start, expanded_end),
            ).fetchall()
            news_rows = database.execute(
                "SELECT * FROM news_events WHERE published_at BETWEEN ? AND ? ORDER BY published_at",
                (expanded_start, expanded_end),
            ).fetchall()
        events = tuple(dict(row) for row in event_rows)
        news = tuple(dict(row) for row in news_rows)
        coverage = context_coverage()
        return cls(
            events=events,
            event_times=tuple(_timestamp_ns(row["scheduled_at"]) or 0 for row in events),
            news=news,
            news_times=tuple(_timestamp_ns(row["published_at"]) or 0 for row in news),
            coverage=coverage,
            calendar_covered=_range_covered(coverage["economicCalendar"], start_at, end_at),
            news_covered=_range_covered(coverage["news"], start_at, end_at),
        )

    def snapshot(self, timestamp_ns: int) -> dict[str, Any]:
        pre_ns = HIGH_IMPACT_PRE_MINUTES * 60 * 1_000_000_000
        post_ns = HIGH_IMPACT_POST_MINUTES * 60 * 1_000_000_000
        left = bisect_left(self.event_times, timestamp_ns - post_ns)
        right = bisect_right(self.event_times, timestamp_ns + pre_ns)
        nearby: list[dict[str, Any]] = []
        event_blocked = False
        for row, scheduled_ns in zip(self.events[left:right], self.event_times[left:right]):
            published_ns = _timestamp_ns(row.get("published_at")) or scheduled_ns
            actual_visible = timestamp_ns >= published_ns
            forecast = row.get("forecast")
            actual = row.get("actual") if actual_visible else None
            surprise = (float(actual) - float(forecast)) if actual is not None and forecast is not None else None
            seconds = round((scheduled_ns - timestamp_ns) / 1_000_000_000, 3)
            high_impact = row.get("importance") == "high" and row.get("currency") in {"USD", "US"}
            if high_impact and -HIGH_IMPACT_POST_MINUTES * 60 <= seconds <= HIGH_IMPACT_PRE_MINUTES * 60:
                event_blocked = True
            nearby.append(
                {
                    "sourceId": row.get("source_id"),
                    "scheduledAt": row.get("scheduled_at"),
                    "eventName": row.get("event_name"),
                    "currency": row.get("currency"),
                    "importance": row.get("importance"),
                    "secondsToEvent": seconds,
                    "forecast": forecast,
                    "actual": actual,
                    "previous": row.get("previous"),
                    "surprise": surprise,
                    "actualAvailable": actual_visible,
                }
            )

        news_left = bisect_left(self.news_times, timestamp_ns - 15 * 60 * 1_000_000_000)
        news_right = bisect_right(self.news_times, timestamp_ns)
        recent_news: list[dict[str, Any]] = []
        news_blocked = False
        for row, published_ns in zip(self.news[news_left:news_right], self.news_times[news_left:news_right]):
            age_seconds = round((timestamp_ns - published_ns) / 1_000_000_000, 3)
            relevance = float(row.get("relevance") or 0)
            if relevance >= 0.8 and age_seconds <= NEWS_BLOCK_SECONDS:
                news_blocked = True
            recent_news.append(
                {
                    "sourceId": row.get("source_id"),
                    "publishedAt": row.get("published_at"),
                    "headline": row.get("headline"),
                    "provider": row.get("provider"),
                    "relevance": relevance,
                    "sentiment": float(row.get("sentiment") or 0),
                    "ageSeconds": age_seconds,
                }
            )
        gate_reasons: list[str] = []
        if event_blocked:
            gate_reasons.append("HIGH_IMPACT_EVENT_WINDOW")
        if news_blocked:
            gate_reasons.append("BREAKING_NEWS_WINDOW")
        return {
            "calendarCoverage": "complete" if self.calendar_covered else "missing",
            "newsCoverage": "complete" if self.news_covered else "missing",
            "nearbyEconomicEvents": nearby,
            "recentNews": recent_news[-10:],
            "eventRisk": "blocked" if event_blocked else "clear",
            "newsRisk": "blocked" if news_blocked else "clear",
            "gate": "blocked" if gate_reasons else "clear",
            "gateReasons": gate_reasons,
            "pointInTimeSafe": True,
        }
