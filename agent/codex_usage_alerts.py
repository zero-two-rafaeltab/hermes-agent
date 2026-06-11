"""Passive Codex account-usage alerts for the messaging gateway.

The gateway calls this after a visible assistant response has already been
sent.  The network fetch is scheduled in a background task by gateway.run; this
module keeps the quota-threshold/no-spam state small and testable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow, fetch_account_usage
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

STATE_FILENAME = "codex_usage_alerts.json"
DEFAULT_THRESHOLD_PERCENT = 80.0
DEFAULT_MIN_CHECK_INTERVAL_SECONDS = 120.0
_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class CodexUsageAlert:
    """One usage window that crossed the configured threshold."""

    label: str
    used_percent: float
    remaining_percent: float
    reset_at: Optional[datetime]


def state_path() -> Path:
    return get_hermes_home() / STATE_FILENAME


def load_state(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or state_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("Failed to read Codex usage alert state", exc_info=True)
        return {}


def save_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    path = path or state_path()
    try:
        atomic_json_write(path, state, indent=2, sort_keys=True)
    except Exception:
        logger.debug("Failed to write Codex usage alert state", exc_info=True)


def _reset_key(reset_at: Optional[datetime]) -> str:
    if reset_at is None:
        return "unknown"
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return reset_at.astimezone(timezone.utc).isoformat()


def _window_key(window: AccountUsageWindow) -> str:
    # Label is the stable API-facing distinction (Session vs Weekly).  Keep it
    # human-readable in the state file so users can inspect it if needed.
    return str(window.label or "window").strip() or "window"


def evaluate_alerts(
    snapshot: Optional[AccountUsageSnapshot],
    state: Optional[dict[str, Any]],
    *,
    threshold_percent: float = DEFAULT_THRESHOLD_PERCENT,
    now: Optional[float] = None,
) -> tuple[list[CodexUsageAlert], dict[str, Any]]:
    """Return newly-triggered alerts and updated no-spam state.

    A window alerts once per reset window.  If Session crosses first, only
    Session is marked.  Weekly can still alert later in the same account state.
    When a reset timestamp changes, that window becomes eligible again.
    """
    state = dict(state or {})
    notified = state.get("notified_windows")
    if not isinstance(notified, dict):
        notified = {}

    alerts: list[CodexUsageAlert] = []
    ts = float(time.time() if now is None else now)
    threshold = float(threshold_percent)

    if snapshot is None:
        state["notified_windows"] = notified
        state["last_evaluated_at"] = ts
        return alerts, state

    for window in snapshot.windows or ():
        if window.used_percent is None:
            continue
        try:
            used = float(window.used_percent)
        except (TypeError, ValueError):
            continue
        if used < threshold:
            continue

        key = _window_key(window)
        reset_key = _reset_key(window.reset_at)
        existing = notified.get(key)
        if isinstance(existing, dict) and existing.get("reset_at") == reset_key:
            # Already notified for this label in this reset window.  Do not
            # re-alert even if usage rises from e.g. 81% to 95%.
            continue

        alert = CodexUsageAlert(
            label=key,
            used_percent=used,
            remaining_percent=max(0.0, 100.0 - used),
            reset_at=window.reset_at,
        )
        alerts.append(alert)
        notified[key] = {
            "reset_at": reset_key,
            "used_percent": used,
            "threshold_percent": threshold,
            "notified_at": ts,
        }

    state["notified_windows"] = notified
    state["last_evaluated_at"] = ts
    return alerts, state


def should_check_now(
    state: Optional[dict[str, Any]],
    *,
    min_interval_seconds: float = DEFAULT_MIN_CHECK_INTERVAL_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Throttle gateway-triggered checks without affecting no-spam semantics."""
    state = state or {}
    ts = float(time.time() if now is None else now)
    try:
        last = float(state.get("last_checked_at") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    return ts - last >= max(0.0, float(min_interval_seconds))


def mark_checked(state: Optional[dict[str, Any]], *, now: Optional[float] = None) -> dict[str, Any]:
    state = dict(state or {})
    state["last_checked_at"] = float(time.time() if now is None else now)
    return state


def _format_reset(reset_at: Optional[datetime]) -> str:
    if not reset_at:
        return "unknown"
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = reset_at.astimezone(timezone.utc) - now
    total = int(delta.total_seconds())
    local = reset_at.astimezone()
    if total <= 0:
        return f"now ({local.strftime('%Y-%m-%d %H:%M %Z')})"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        rel = f"in {days}d {hours}h"
    elif hours:
        rel = f"in {hours}h {minutes}m"
    else:
        rel = f"in {minutes}m"
    return f"{rel} ({local.strftime('%Y-%m-%d %H:%M %Z')})"


def render_alert_message(
    alerts: list[CodexUsageAlert],
    snapshot: Optional[AccountUsageSnapshot],
    *,
    threshold_percent: float = DEFAULT_THRESHOLD_PERCENT,
) -> str:
    """Render a compact Discord/Telegram-safe alert message."""
    if not alerts:
        return ""
    plan = f" ({snapshot.plan})" if snapshot and snapshot.plan else ""
    lines = [f"⚠️ **OpenAI Codex usage alert**{plan}"]
    threshold = int(threshold_percent) if float(threshold_percent).is_integer() else threshold_percent
    for alert in alerts:
        lines.append(
            f"{alert.label}: {alert.used_percent:.0f}% used "
            f"({alert.remaining_percent:.0f}% remaining) — crossed {threshold}% threshold; "
            f"resets {_format_reset(alert.reset_at)}"
        )

    if snapshot:
        other_parts: list[str] = []
        alert_labels = {a.label for a in alerts}
        for window in snapshot.windows or ():
            if window.used_percent is None or window.label in alert_labels:
                continue
            try:
                used = float(window.used_percent)
            except (TypeError, ValueError):
                continue
            other_parts.append(f"{window.label}: {used:.0f}% used")
        if other_parts:
            lines.append("Other windows: " + " • ".join(other_parts))
    return "\n".join(lines)


def fetch_evaluate_and_render(
    *,
    threshold_percent: float = DEFAULT_THRESHOLD_PERCENT,
    min_interval_seconds: float = DEFAULT_MIN_CHECK_INTERVAL_SECONDS,
    path: Optional[Path] = None,
) -> str:
    """Fetch Codex usage, update state, and return alert text if newly crossed.

    Returns an empty string when throttled, unavailable, below threshold, or
    already-notified for the current reset window.
    """
    path = path or state_path()
    with _STATE_LOCK:
        state = load_state(path)
        if not should_check_now(state, min_interval_seconds=min_interval_seconds):
            return ""
        state = mark_checked(state)
        save_state(state, path)

        snapshot = fetch_account_usage("openai-codex")
        alerts, state = evaluate_alerts(
            snapshot,
            state,
            threshold_percent=threshold_percent,
        )
        save_state(state, path)
    if not alerts:
        return ""
    return render_alert_message(alerts, snapshot, threshold_percent=threshold_percent)
