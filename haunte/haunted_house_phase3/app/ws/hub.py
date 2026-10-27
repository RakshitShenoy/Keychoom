"""Realtime hub: authentication, snapshots, presence and bus -> WebSocket fan-out.

Flow for every state change:   REST request -> validate -> DB commit -> LobbyEventBus ->
RealtimeHub.on_event -> (event message now) + (personalised LOBBY_STATE shortly after).
The database is always read AFTER the commit, so a snapshot can never show something that was
not saved. The WebSocket never changes state itself.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from sqlalchemy import update

from app.auth.dependencies import identify_viewer
from app.db.models import Game, Player
from app.lobby.chat import recent_messages
from app.lobby.events import LobbyEvent, LobbyEventBus, LobbyEventType
from app.lobby.views import Viewer, build_lobby_view
from app.teams.queries import load_lobby_data
from app.ws.manager import ConnectionManager
from app.ws.protocol import (
    CLOSE_FORBIDDEN, CLOSE_NORMAL, error_message, event_message, public_payload, state_message,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from app.config import Settings

log = logging.getLogger(__name__)

KICKED_ERROR = ("KICKED", "You were removed from the game by the host.")
NOT_AUTHORIZED_ERROR = ("NOT_AUTHORIZED", "You are not authorized to access this game.")


@dataclass(frozen=True)
class SocketIdentity:
    role: str
    game_id: str
    player_id: str | None


class RealtimeHub:
    def __init__(
        self, settings: "Settings", session_factory: "sessionmaker", bus: LobbyEventBus
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bus = bus
        self.manager = ConnectionManager()
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._closed = False
        bus.subscribe(self.on_event)

    # ------------------------------------------------------------------ authentication
    def authenticate(
        self, cookies: Mapping[str, str], game_code: str, role: str | None
    ) -> SocketIdentity:
        """Same rules as the REST API (``identify_viewer``). Raises AppError when refused."""
        db = self.session_factory()
        try:
            viewer = identify_viewer(db, self.settings, cookies, game_code, role)
            return SocketIdentity(
                role=viewer.role,
                game_id=viewer.game.id,
                player_id=viewer.player.id if viewer.player else None,
            )
        finally:
            db.close()

    # ------------------------------------------------------------------ snapshots
    def _next_version(self, game_id: str) -> int:
        with self._lock:
            self._versions[game_id] = self._versions.get(game_id, 0) + 1
            return self._versions[game_id]

    def snapshot_for(
        self, game_id: str, role: str, player_id: str | None
    ) -> tuple[int, dict[str, Any], str | None] | None:
        """Fresh authoritative state for ONE viewer, or None if they no longer belong here."""
        version = self._next_version(game_id)  # taken BEFORE reading: newer version => newer data
        db = self.session_factory()
        try:
            game = db.get(Game, game_id, populate_existing=True)
            if game is None:
                return None
            data = load_lobby_data(db, game)
            player = None
            if role == "player":
                player = next((p for p in data.players if p.id == player_id), None)
                if player is None:
                    return None
            view = build_lobby_view(
                db, self.settings, Viewer(role, game, player), data=data,
                messages=recent_messages(db, game, self.settings.chat_history_limit),
            )
            return version, view.model_dump(mode="json"), (player.team_id if player else None)
        finally:
            db.close()

    def push_snapshots(self, game_id: str) -> None:
        """Send every connected client of the game its own up-to-date LOBBY_STATE."""
        conns = self.manager.connections(game_id)
        if not conns:
            return
        version = self._next_version(game_id)
        db = self.session_factory()
        try:
            game = db.get(Game, game_id, populate_existing=True)
            if game is None:
                for conn in conns:
                    conn.close(CLOSE_FORBIDDEN, error_message(*NOT_AUTHORIZED_ERROR))
                return
            data = load_lobby_data(db, game)  # ONE load for all viewers
            messages = recent_messages(db, game, self.settings.chat_history_limit)
            players = {p.id: p for p in data.players}
            host_message = None
            player_messages: dict[str, dict] = {}
            for conn in conns:
                if conn.role == "host":
                    if host_message is None:
                        view = build_lobby_view(
                            db, self.settings, Viewer("host", game), data=data, messages=messages
                        )
                        host_message = state_message(version, view.model_dump(mode="json"))
                    conn.send(host_message)
                    continue
                player = players.get(conn.player_id)
                if player is None:  # kicked / left: this session no longer exists
                    conn.close(CLOSE_FORBIDDEN, error_message(*KICKED_ERROR))
                    continue
                conn.team_id = player.team_id
                if player.id not in player_messages:
                    view = build_lobby_view(
                        db, self.settings, Viewer("player", game, player), data=data, messages=messages
                    )
                    player_messages[player.id] = state_message(version, view.model_dump(mode="json"))
                conn.send(player_messages[player.id])
        finally:
            db.close()

    def _schedule_snapshot(self, game_id: str) -> None:
        """Coalesce bursts: many changes within the debounce window produce one push."""
        if not self.manager.connections(game_id):
            return  # nobody is listening: no need to read the database
        with self._lock:
            if self._closed or game_id in self._timers:
                return
            timer = threading.Timer(
                self.settings.ws_snapshot_debounce_seconds, self._flush, args=(game_id,)
            )
            timer.daemon = True
            self._timers[game_id] = timer
        timer.start()

    def _flush(self, game_id: str) -> None:
        with self._lock:
            self._timers.pop(game_id, None)
        try:
            self.push_snapshots(game_id)
        except Exception:  # never let a push failure kill the timer thread silently
            log.exception("snapshot push failed for game %s", game_id)

    # ------------------------------------------------------------------ bus -> sockets
    def on_event(self, event: LobbyEvent) -> None:
        try:
            self._on_event(event)
        except Exception:
            log.exception("realtime fan-out failed for %s", event.type)

    def _on_event(self, event: LobbyEvent) -> None:
        game_id = event.game_id
        if event.type is LobbyEventType.LOBBY_UPDATED:
            self._schedule_snapshot(game_id)
            return

        name = event.type.value
        # Hosts get the full payload; players get it without row ids.
        self.manager.send_game(game_id, event_message(name, dict(event.payload)), role="host")
        self.manager.send_game(
            game_id, event_message(name, public_payload(event.payload)), role="player"
        )

        player_id = event.payload.get("player_id")
        if player_id and event.type is LobbyEventType.PLAYER_KICKED:
            self.manager.close_player(
                game_id, player_id, CLOSE_FORBIDDEN, error_message(*KICKED_ERROR)
            )
        elif player_id and event.type is LobbyEventType.PLAYER_LEFT:
            self.manager.close_player(game_id, player_id, CLOSE_NORMAL)

    # ------------------------------------------------------------------ presence
    def set_presence(self, game_id: str, player_id: str, connected: bool) -> None:
        """Record connected/disconnected (membership is NEVER touched) and announce it."""
        db = self.session_factory()
        try:
            player = db.get(Player, player_id, populate_existing=True)
            if player is None or player.game_id != game_id:
                return  # e.g. kicked in the meantime
            player.is_connected = connected
            name = player.display_name
            db.commit()
        finally:
            db.close()
        kind = LobbyEventType.PLAYER_CONNECTED if connected else LobbyEventType.PLAYER_DISCONNECTED
        self.bus.publish_change(
            LobbyEvent(kind, game_id, {"player_id": player_id, "display_name": name})
        )

    def reset_presence(self) -> None:
        """After a restart no browser is connected: clear stale flags."""
        db = self.session_factory()
        try:
            db.execute(update(Player).where(Player.is_connected.is_(True)).values(is_connected=False))
            db.commit()
        finally:
            db.close()

    def start(self) -> None:
        with self._lock:
            self._closed = False

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()
