from __future__ import annotations

import datetime as dt
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ACCOUNT_TYPES, PAY_FREQUENCIES, TAX_LINES, Company
from ..money import amount_for_minutes, cents_to_str, fmt_hours, fmt_money
from ..tax.tables import FILING_STATUSES

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = fmt_money
templates.env.filters["hours"] = fmt_hours
templates.env.filters["cents"] = cents_to_str
templates.env.filters["date"] = lambda d: d.strftime("%b %d, %Y") if d else ""
templates.env.globals.update(
    ACCOUNT_TYPES=ACCOUNT_TYPES,
    TAX_LINES=TAX_LINES,
    amount_for_minutes=amount_for_minutes,
    PAY_FREQUENCIES=PAY_FREQUENCIES,
    FILING_STATUSES=FILING_STATUSES,
)


def get_db(request: Request):
    db: Session = request.app.state.sessionmaker()
    try:
        yield db
    finally:
        db.close()


def get_company(db: Session) -> Company:
    company = db.scalar(select(Company).limit(1))
    if company is None:
        company = Company()
        db.add(company)
        db.commit()
    return company


def render(request: Request, name: str, **ctx):
    ctx.setdefault("today", dt.date.today())
    ctx["msg"] = request.query_params.get("msg")
    ctx["err"] = request.query_params.get("err")
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    if msg or err:
        sep = "&" if "?" in url else "?"
        url += sep + ("msg=" + quote(msg) if msg else "err=" + quote(err))
    return RedirectResponse(url, status_code=303)


def parse_date(text: str | None, default: dt.date | None = None) -> dt.date:
    if not text:
        if default is None:
            raise ValueError("Date is required")
        return default
    return dt.date.fromisoformat(text)


def opt_int(text: str | None) -> int | None:
    return int(text) if text not in (None, "") else None


def checked(form, name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def spend_accounts(db: Session):
    """Accounts money can be spent to, expenses first (for dropdowns)."""
    from ..models import Account

    order = {"expense": 0, "equity": 1, "liability": 2, "asset": 3, "income": 4}
    accts = [a for a in db.scalars(select(Account).where(Account.active.is_(True)))
             if a.code not in ("ar", "undeposited", "retained", "opening") and not (a.is_system and a.type == "asset" and not a.code)]
    return sorted(accts, key=lambda a: (order[a.type], a.number))
