from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import Settings, get_settings


@lru_cache(maxsize=8)
def get_engine(database_url: str, echo: bool = False) -> Engine:
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    resolved = settings or get_settings()
    return sessionmaker(
        bind=get_engine(resolved.database_url, resolved.database_echo),
        autoflush=False,
        expire_on_commit=False,
    )


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    session = get_session_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

