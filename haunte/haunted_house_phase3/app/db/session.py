from __future__ import annotations

from typing import Iterator

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import make_engine


class Database:
    """Owns the engine and session factory for one application instance."""

    def __init__(self, url: str) -> None:
        self.engine: Engine = make_engine(url)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=Session
        )

    def dispose(self) -> None:
        self.engine.dispose()


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    session = request.app.state.db.session_factory()
    try:
        yield session
    finally:
        session.close()
