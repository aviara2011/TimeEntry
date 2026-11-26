import datetime as dt

from timeentry.db import make_engine, make_sessionmaker
from timeentry.demo import build_demo
from timeentry.ledger import Books, balance_sheet


def test_demo_builds_and_balances(tmp_path):
    path = tmp_path / "demo.db"
    build_demo(path, today=dt.date(2026, 11, 26))
    db = make_sessionmaker(make_engine(f"sqlite:///{path}"))()
    for basis in ("cash", "accrual"):
        assert balance_sheet(Books(db, basis), dt.date(2026, 11, 26))["balanced"]
