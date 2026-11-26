from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def default_db_path() -> Path:
    return Path(os.environ.get("TIMEENTRY_DB", Path(__file__).resolve().parent.parent / "data" / "timeentry.db"))


def make_engine(url: str | None = None):
    if url is None:
        path = default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def make_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine):
    from . import models  # noqa: F401  (register tables)
    from .coa import backfill, migrate

    Base.metadata.create_all(engine)
    migrate(engine)
    with make_sessionmaker(engine)() as db:
        backfill(db)
