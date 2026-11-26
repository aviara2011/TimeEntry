from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..db import init_db, make_engine, make_sessionmaker
from . import routes_bank, routes_core, routes_invoices, routes_payroll, routes_taxes, routes_time


def create_app(db_url: str | None = None) -> FastAPI:
    app = FastAPI(title="TimeEntry", docs_url=None, redoc_url=None)
    engine = make_engine(db_url)
    init_db(engine)
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    for module in (routes_bank, routes_core, routes_time, routes_invoices, routes_payroll, routes_taxes):
        app.include_router(module.router)
    return app
