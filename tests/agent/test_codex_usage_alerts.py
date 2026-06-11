from datetime import datetime, timezone

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.codex_usage_alerts import evaluate_alerts, should_check_now


def _snapshot(*windows):
    return AccountUsageSnapshot(
        provider="openai-codex",
        source="test",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        plan="Plus",
        windows=tuple(windows),
    )


def test_no_alert_below_threshold():
    snap = _snapshot(
        AccountUsageWindow("Session", used_percent=79.9, reset_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc)),
        AccountUsageWindow("Weekly", used_percent=8, reset_at=datetime(2026, 1, 8, tzinfo=timezone.utc)),
    )

    alerts, state = evaluate_alerts(snap, {}, threshold_percent=80, now=100)

    assert alerts == []
    assert state["notified_windows"] == {}


def test_alerts_once_per_label_and_reset_window():
    reset = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    snap = _snapshot(AccountUsageWindow("Session", used_percent=80, reset_at=reset))

    alerts, state = evaluate_alerts(snap, {}, threshold_percent=80, now=100)
    assert [a.label for a in alerts] == ["Session"]

    alerts, state = evaluate_alerts(snap, state, threshold_percent=80, now=200)
    assert alerts == []


def test_different_limit_can_alert_after_first_limit_already_notified():
    session_reset = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    weekly_reset = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
    session_only = _snapshot(
        AccountUsageWindow("Session", used_percent=85, reset_at=session_reset),
        AccountUsageWindow("Weekly", used_percent=50, reset_at=weekly_reset),
    )
    alerts, state = evaluate_alerts(session_only, {}, threshold_percent=80, now=100)
    assert [a.label for a in alerts] == ["Session"]

    weekly_crossed_later = _snapshot(
        AccountUsageWindow("Session", used_percent=95, reset_at=session_reset),
        AccountUsageWindow("Weekly", used_percent=82, reset_at=weekly_reset),
    )
    alerts, state = evaluate_alerts(weekly_crossed_later, state, threshold_percent=80, now=200)
    assert [a.label for a in alerts] == ["Weekly"]


def test_same_limit_alerts_again_after_reset_window_changes():
    old_reset = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    new_reset = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)

    alerts, state = evaluate_alerts(
        _snapshot(AccountUsageWindow("Session", used_percent=90, reset_at=old_reset)),
        {},
        threshold_percent=80,
        now=100,
    )
    assert [a.label for a in alerts] == ["Session"]

    alerts, state = evaluate_alerts(
        _snapshot(AccountUsageWindow("Session", used_percent=81, reset_at=new_reset)),
        state,
        threshold_percent=80,
        now=200,
    )
    assert [a.label for a in alerts] == ["Session"]


def test_check_throttle():
    assert should_check_now({"last_checked_at": 100}, min_interval_seconds=120, now=219) is False
    assert should_check_now({"last_checked_at": 100}, min_interval_seconds=120, now=220) is True
