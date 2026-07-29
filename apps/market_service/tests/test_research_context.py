from __future__ import annotations

from datetime import datetime

import pytest

from apps.market_service.research_context import HistoricalContextIndex


def ns(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


def test_context_snapshot_prevents_lookahead_and_blocks_high_impact_window() -> None:
    event = {
        "source_id": "cpi", "scheduled_at": "2026-07-14T12:30:00Z", "published_at": "2026-07-14T12:30:02Z",
        "event_name": "CPI", "currency": "USD", "importance": "high", "forecast": 3.0, "actual": 3.2, "previous": 3.1,
    }
    news = {
        "source_id": "headline", "published_at": "2026-07-14T12:31:00Z", "headline": "Policy headline",
        "provider": "fixture", "relevance": 0.9, "sentiment": -0.5,
    }
    index = HistoricalContextIndex(
        events=(event,), event_times=(ns(event["scheduled_at"]),),
        news=(news,), news_times=(ns(news["published_at"]),),
        coverage={}, calendar_covered=True, news_covered=True,
    )

    before_release = index.snapshot(ns("2026-07-14T12:30:01Z"))
    assert before_release["gate"] == "blocked"
    assert before_release["nearbyEconomicEvents"][0]["actual"] is None
    assert before_release["nearbyEconomicEvents"][0]["actualAvailable"] is False

    after_release = index.snapshot(ns("2026-07-14T12:31:30Z"))
    assert after_release["nearbyEconomicEvents"][0]["actual"] == 3.2
    assert after_release["nearbyEconomicEvents"][0]["surprise"] == pytest.approx(0.2)
    assert after_release["newsRisk"] == "blocked"
    assert after_release["recentNews"][0]["headline"] == "Policy headline"


def test_research_readiness_requires_rows_and_full_interval_coverage() -> None:
    from apps.market_service.research import _research_readiness

    datasets = []
    for month in range(1, 7):
        for day in range(1, 18):
            datasets.append({
                "start_at": f"2026-{month:02d}-{day:02d}T14:00:00Z",
                "end_at": f"2026-{month:02d}-{day:02d}T21:00:00Z",
                "completeness": "complete",
                "integrity_status": "passed",
                "data_mode": "full_l3",
                "record_count": 1,
            })
    missing_rows = {
        "economicCalendar": {"declaredCoverage": True, "coverageStart": "2026-01-01T00:00:00Z", "coverageEnd": "2026-07-01T00:00:00Z", "rowCount": 0},
        "news": {"declaredCoverage": True, "coverageStart": "2026-01-01T00:00:00Z", "coverageEnd": "2026-07-01T00:00:00Z", "rowCount": 0},
    }
    blocked = _research_readiness(datasets, missing_rows)
    assert "CALENDAR_COVERAGE_MISSING" in blocked["blockers"]
    assert "NEWS_COVERAGE_MISSING" in blocked["blockers"]

    complete = {
        "economicCalendar": {**missing_rows["economicCalendar"], "rowCount": 1200},
        "news": {**missing_rows["news"], "rowCount": 5000},
    }
    ready = _research_readiness(datasets, complete)
    assert ready["readyForValidatedSignals"] is True
    assert ready["signalMode"] == "PAPER_REPLAY_ONLY"
