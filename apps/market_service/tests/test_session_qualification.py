from apps.market_service.session_qualification import (
    independent_session_rejection_reason,
    qualifying_independent_full_l3_sessions,
)


def dataset(**overrides):
    value = {
        "id": "session",
        "completeness": "complete",
        "integrity_status": "passed",
        "data_mode": "full_l3",
        "data_health": {"fullL3Claim": True},
        "start_at": "2026-07-14T00:00:00Z",
        "end_at": "2026-07-14T14:30:00Z",
        "record_count": 8_167_554,
    }
    value.update(overrides)
    return value


def test_large_full_l3_session_qualifies():
    item = dataset()
    assert independent_session_rejection_reason(item) is None
    assert qualifying_independent_full_l3_sessions([item]) == [item]


def test_tiny_snapshot_does_not_count_as_independent_day():
    item = dataset(
        id="tiny",
        start_at="2026-07-13T23:59:55Z",
        end_at="2026-07-14T00:00:09Z",
        record_count=15_343,
    )
    assert independent_session_rejection_reason(item) == "TOO_SHORT_FOR_INDEPENDENT_DAY"
    assert qualifying_independent_full_l3_sessions([item]) == []


def test_partial_orderflow_does_not_qualify():
    item = dataset(
        completeness="partial",
        data_mode="orderflow_partial",
        data_health={"fullL3Claim": False},
    )
    assert independent_session_rejection_reason(item) == "NOT_COMPLETE"


def test_six_hour_regular_session_qualifies():
    item = dataset(end_at="2026-07-14T06:00:00Z", record_count=1)
    assert independent_session_rejection_reason(item) is None


def test_just_under_six_hours_is_rejected():
    item = dataset(end_at="2026-07-14T05:59:59Z")
    assert independent_session_rejection_reason(item) == "TOO_SHORT_FOR_INDEPENDENT_DAY"
