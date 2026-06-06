"""Public gateway child-session extension seam types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time only typing aid
    from gateway.platforms.base import MessageEvent


@dataclass(frozen=True)
class GatewayChildSessionRequest:
    """Request for a gateway plugin to start a platform-native child session.

    The parent event must be a normal, already-authorized gateway event. The
    runner re-checks that authorization before creating any destination so a
    plugin cannot bypass Discord/gateway access policy by forging a synthetic
    child event.
    """

    parent_event: "MessageEvent"
    child_title: str
    starter_prompt: str
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayChildSessionResult:
    """Identifiers returned after starting (or replaying) a child session."""

    platform: str
    parent_channel_id: str
    child_channel_id: str
    thread_name: str
    session_key: Optional[str]
    session_id: Optional[str]
    scheduled_started: bool
    idempotent_replay: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
