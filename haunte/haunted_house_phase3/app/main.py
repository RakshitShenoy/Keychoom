"""Application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth.ratelimit import FailureLimiter
from app.auth.router import router as session_router
from app.config import STATIC_DIR, TEMPLATES_DIR, Settings, get_settings
from app.content.loader import load_and_validate
from app.db.session import Database
from app.error_handlers import register_error_handlers
from app.host.router import router as host_router
from app.lobby.events import LobbyEventBus
from app.lobby.lobby_router import router as lobby_state_router
from app.lobby.pages import router as pages_router
from app.lobby.router import router as lobby_router
from app.teams.router import games as team_games_router
from app.ws.hub import RealtimeHub
from app.ws.router import router as ws_router
from app.teams.router import router as player_router

log = logging.getLogger("haunted_house")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Invalid game content must stop the server from starting.
        app.state.content = load_and_validate(settings.content_dir)
        app.state.realtime.start()
        try:
            app.state.realtime.reset_presence()  # nobody is connected after a (re)start
        except Exception:  # e.g. migrations not applied yet: do not hide the real error elsewhere
            log.warning("could not reset player presence at startup", exc_info=True)
        log.info("startup complete env=%s", settings.app_env)
        yield
        app.state.realtime.shutdown()
        app.state.db.dispose()

    app = FastAPI(title="Escape the Haunted House", version="0.1.0-phase1", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = Database(settings.database_url)
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.join_failure_limiter = FailureLimiter(
        settings.join_failure_limit, settings.join_failure_window_seconds
    )

    app.state.lobby_bus = LobbyEventBus()  # Phase 3 subscribes the WebSocket layer here
    app.state.team_code_limiter = FailureLimiter(
        settings.team_code_failure_limit, settings.team_code_failure_window_seconds
    )
    app.state.chat_limiter = FailureLimiter(
        settings.chat_rate_limit_messages, settings.chat_rate_limit_window_seconds
    )

    app.state.realtime = RealtimeHub(settings, app.state.db.session_factory, app.state.lobby_bus)

    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(pages_router)
    app.include_router(lobby_router)
    app.include_router(session_router)
    app.include_router(host_router)
    app.include_router(player_router)
    app.include_router(lobby_state_router)
    app.include_router(team_games_router)
    app.include_router(ws_router)
    return app


app = create_app()
