"""Lobby events.

Every meaningful change is (a) written to the ``game_events`` audit table inside the same
transaction as the change and (b) announced on an in-process bus *after* the commit.
Phase 2 has no subscribers; Phase 3 will subscribe the WebSocket layer here, so REST handlers
and WebSocket handlers never duplicate state logic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import GameEvent
from app.timers.clock import utcnow

log = logging.getLogger(__name__)


class LobbyEventType(str, Enum):
    PLAYER_JOINED = "PLAYER_JOINED"
    PLAYER_LEFT = "PLAYER_LEFT"
    PLAYER_KICKED = "PLAYER_KICKED"
    LEADER_ASSIGNED = "LEADER_ASSIGNED"
    LEADER_REMOVED = "LEADER_REMOVED"
    TEAM_JOINED = "TEAM_JOINED"
    TEAM_UPDATED = "TEAM_UPDATED"
    TEAM_FORMATION_STARTED = "TEAM_FORMATION_STARTED"
    TEAM_FORMATION_LOCKED = "TEAM_FORMATION_LOCKED"
    TEAM_FORMATION_REOPENED = "TEAM_FORMATION_REOPENED"
    PLAYER_CONNECTED = "PLAYER_CONNECTED"
    PLAYER_DISCONNECTED = "PLAYER_DISCONNECTED"
    CHAT_MESSAGE = "CHAT_MESSAGE"
    LOBBY_UPDATED = "LOBBY_UPDATED"


@dataclass(frozen=True)
class LobbyEvent:
    type: LobbyEventType
    game_id: str
    payload: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[LobbyEvent], None]


class LobbyEventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, event: LobbyEvent) -> None:
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:  # a broken listener must never break a request
                log.exception("lobby event subscriber failed for %s", event.type)

    def publish_change(self, event: LobbyEvent, *also: LobbyEvent) -> None:
        """Publish the specific event(s), then a generic LOBBY_UPDATED ("state changed")."""
        self.publish(event)
        for extra in also:
            self.publish(extra)
        if event.type is not LobbyEventType.LOBBY_UPDATED:
            self.publish(LobbyEvent(LobbyEventType.LOBBY_UPDATED, event.game_id))


def record_event(
    db: Session,
    game_id: str,
    event_type: LobbyEventType,
    payload: dict[str, Any] | None = None,
    team_id: str | None = None,
) -> LobbyEvent:
    """Add the audit row to the current transaction (caller commits) and return the event."""
    data = payload or {}
    db.add(
        GameEvent(
            game_id=game_id,
            team_id=team_id,
            event_type=event_type.value,
            payload=data,
            created_at=utcnow(),
        )
    )
    return LobbyEvent(event_type, game_id, data)
