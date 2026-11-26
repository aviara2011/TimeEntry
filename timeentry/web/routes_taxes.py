"""Federal tax worksheets (941, 940, W-2/W-3, 1120-S) and EFTPS deposits."""
from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TaxDeposit
from ..money import parse_money
from ..reports import LINE_LABELS_1120S, form_940, form_941, form_1120s, w2s, w3_totals
from ..tax.tables import TAX_YEARS
from .common import get_company, get_db, opt_int, parse_date, redirect, render

router = APIRouter(prefix="/taxes")


def _year(request: Request) -> int:
    return int(request.query_params.get("year") or dt.date.today().year)


@router.get("")
def taxes(request: Request, db: Session = Depends(get_db)):
    year = _year(request)
    quarters = [form_941(db, year, q) for q in range(1, 5)]
    deposits = db.scalars(select(TaxDeposit).where(TaxDeposit.year == year).order_by(TaxDeposit.date)).all()
    return render(request, "taxes.html", year=year, quarters=quarters, f940=form_940(db, year),
                  deposits=deposits, company=get_company(db), years=sorted(set(TAX_YEARS) | {year}))


@router.get("/941")
def view_941(request: Request, db: Session = Depends(get_db)):
    year = _year(request)
    quarter = int(request.query_params.get("quarter") or ((dt.date.today().month - 1) // 3 + 1))
    return render(request, "form941.html", f=form_941(db, year, quarter), company=get_company(db))


@router.get("/940")
def view_940(request: Request, db: Session = Depends(get_db)):
    return render(request, "form940.html", f=form_940(db, _year(request)), company=get_company(db))


@router.get("/w2")
def view_w2(request: Request, db: Session = Depends(get_db)):
    year = _year(request)
    forms = w2s(db, year)
    return render(request, "w2.html", year=year, forms=forms, w3=w3_totals(forms), company=get_company(db))


@router.get("/w2.csv")
def w2_csv(request: Request, db: Session = Depends(get_db)):
    year = _year(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Employee", "SSN", "Box1", "Box2", "Box3", "Box4", "Box5", "Box6", "Box14 S-Corp health"])
    for f in w2s(db, year):
        w.writerow([f.employee.name, f.employee.ssn] + [f"{getattr(f, k) / 100:.2f}" for k in
                    ("box1", "box2", "box3", "box4", "box5", "box6", "box14_health")])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=w2-{year}.csv"})


@router.get("/1120s")
def view_1120s(request: Request, db: Session = Depends(get_db)):
    company = get_company(db)
    return render(request, "form1120s.html", f=form_1120s(db, company, _year(request)),
                  labels=LINE_LABELS_1120S, company=company)


@router.post("/deposits")
async def deposit_add(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    try:
        dep = TaxDeposit(
            date=parse_date(f.get("date"), dt.date.today()), form=f.get("form", "941"),
            year=int(f["year"]), quarter=opt_int(f.get("quarter")) if f.get("form") == "941" else None,
            amount=parse_money(f.get("amount")), confirmation=f.get("confirmation", ""),
        )
        if dep.form not in ("941", "940"):
            raise ValueError("Form must be 941 or 940")
        if dep.form == "941" and dep.quarter not in (1, 2, 3, 4):
            raise ValueError("Choose the quarter this 941 deposit is for")
        if dep.amount <= 0:
            raise ValueError("Amount must be greater than zero")
    except (ValueError, KeyError) as exc:
        return redirect(f"/taxes?year={f.get('year', '')}", err=str(exc))
    db.add(dep)
    db.commit()
    return redirect(f"/taxes?year={dep.year}", msg="Deposit recorded")


@router.post("/deposits/{deposit_id}/delete")
def deposit_delete(deposit_id: int, db: Session = Depends(get_db)):
    dep = db.get(TaxDeposit, deposit_id)
    year = dep.year if dep else dt.date.today().year
    if dep:
        db.delete(dep)
        db.commit()
    return redirect(f"/taxes?year={year}", msg="Deposit deleted")
