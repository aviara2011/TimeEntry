"""Pay runs and paychecks."""
from __future__ import annotations

import calendar
import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, Paycheck, PayRun
from ..money import parse_money
from ..payroll import later_final_runs, recompute_run, salary_per_period
from ..tax.tables import TAX_YEARS
from .common import checked, get_company, get_db, parse_date, redirect, render

router = APIRouter(prefix="/payroll")


def default_period(freq: str, today: dt.date) -> tuple[dt.date, dt.date, dt.date]:
    """(period_start, period_end, pay_date) suggestion for the current period."""
    if freq == "monthly":
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif freq == "semimonthly":
        if today.day <= 15:
            start, end = today.replace(day=1), today.replace(day=15)
        else:
            start = today.replace(day=16)
            end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    else:
        days = 7 if freq == "weekly" else 14
        start = today - dt.timedelta(days=today.weekday())
        end = start + dt.timedelta(days=days - 1)
    return start, end, end


@router.get("")
def payroll_list(request: Request, db: Session = Depends(get_db)):
    company = get_company(db)
    runs = db.scalars(select(PayRun).order_by(PayRun.pay_date.desc(), PayRun.id.desc())).all()
    start, end, pay = default_period(company.pay_frequency, dt.date.today())
    return render(request, "payroll.html", runs=runs, company=company, start=start, end=end, pay=pay,
                  years=sorted(TAX_YEARS))


@router.post("/create")
async def payroll_create(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    company = get_company(db)
    try:
        run = PayRun(
            pay_date=parse_date(f.get("pay_date")), period_start=parse_date(f.get("period_start")),
            period_end=parse_date(f.get("period_end")), status="draft", memo=f.get("memo", ""),
        )
        if run.period_end < run.period_start:
            raise ValueError("Period end is before period start")
        if run.pay_date.year not in TAX_YEARS:
            raise ValueError(f"No tax tables for {run.pay_date.year}; add them in timeentry/tax/tables.py")
    except ValueError as exc:
        return redirect("/payroll", err=str(exc))
    historical = checked(f, "historical")
    db.add(run)
    db.flush()
    employees = db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.last_name)).all()
    if not employees:
        db.rollback()
        return redirect("/payroll", err="Add employees first")
    for emp in employees:
        pc = Paycheck(employee=emp, gross=0 if historical else salary_per_period(company, emp),
                      shareholder_health=0, manual=historical)
        run.paychecks.append(pc)
    db.flush()
    recompute_run(db, company, run)
    db.commit()
    return redirect(f"/payroll/{run.id}", msg="Draft pay run created" + (" — enter amounts from your old payroll" if historical else ""))


@router.get("/{run_id}")
def payroll_view(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(PayRun, run_id)
    if not run:
        return redirect("/payroll", err="Pay run not found")
    return render(request, "payrun.html", run=run, company=get_company(db),
                  later=later_final_runs(db, run) if run.status == "draft" else [])


@router.post("/{run_id}/save")
async def payroll_save(run_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    company = get_company(db)
    run = db.get(PayRun, run_id)
    if run.status != "draft":
        return redirect(f"/payroll/{run_id}", err="Finalized pay runs can't be edited. Reopen it first.")
    try:
        run.pay_date = parse_date(f.get("pay_date"), run.pay_date)
        run.period_start = parse_date(f.get("period_start"), run.period_start)
        run.period_end = parse_date(f.get("period_end"), run.period_end)
        run.memo = f.get("memo", "")
        for pc in run.paychecks:
            k = f"pc{pc.id}_"
            pc.gross = parse_money(f.get(k + "gross"))
            pc.shareholder_health = parse_money(f.get(k + "health"))
            pc.manual = checked(f, k + "manual")
            if pc.manual:
                # Fields only appear once "manual" is on; keep current values until then.
                for field in ("fit", "ss_ee", "medicare_ee", "addl_medicare"):
                    if (k + field) in f:
                        setattr(pc, field, parse_money(f.get(k + field)))
            if pc.gross < 0 or pc.shareholder_health < 0:
                raise ValueError("Amounts can't be negative")
    except ValueError as exc:
        db.rollback()
        return redirect(f"/payroll/{run_id}", err=str(exc))
    recompute_run(db, company, run)
    db.commit()
    return redirect(f"/payroll/{run_id}", msg="Recalculated")


@router.post("/{run_id}/remove/{paycheck_id}")
def paycheck_remove(run_id: int, paycheck_id: int, db: Session = Depends(get_db)):
    run = db.get(PayRun, run_id)
    pc = db.get(Paycheck, paycheck_id)
    if run.status == "draft" and pc and pc.pay_run_id == run.id:
        run.paychecks.remove(pc)
        db.commit()
    return redirect(f"/payroll/{run_id}", msg="Paycheck removed")


@router.post("/{run_id}/finalize")
def payroll_finalize(run_id: int, db: Session = Depends(get_db)):
    company = get_company(db)
    run = db.get(PayRun, run_id)
    if run.status != "draft":
        return redirect(f"/payroll/{run_id}")
    recompute_run(db, company, run)
    run.status = "final"
    db.commit()
    later = later_final_runs(db, run)
    msg = "Pay run finalized. Record the federal tax deposit under Taxes when you pay it through EFTPS."
    if later:
        msg += f" Note: {len(later)} later pay run(s) this year were finalized before this one; reopen and re-finalize them so their wage-base limits are right."
    return redirect(f"/payroll/{run_id}", msg=msg)


@router.post("/{run_id}/reopen")
def payroll_reopen(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PayRun, run_id)
    run.status = "draft"
    db.commit()
    return redirect(f"/payroll/{run_id}", msg="Reopened as draft")


@router.post("/{run_id}/delete")
def payroll_delete(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PayRun, run_id)
    if run and run.status == "draft":
        db.delete(run)
        db.commit()
        return redirect("/payroll", msg="Draft pay run deleted")
    return redirect(f"/payroll/{run_id}", err="Only drafts can be deleted")


@router.get("/{run_id}/stubs")
def paystubs(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(PayRun, run_id)
    stubs = []
    for pc in run.paychecks:
        prior = [p for p, r in _prior_checks(db, pc.employee_id, run)]
        ytd = {k: sum(getattr(p, k) for p in prior) + getattr(pc, k)
               for k in ("gross", "shareholder_health", "fit", "ss_ee", "medicare_ee", "addl_medicare")}
        ytd["net"] = sum(p.net for p in prior) + pc.net
        stubs.append((pc, ytd))
    return render(request, "paystubs.html", run=run, stubs=stubs, company=get_company(db))


def _prior_checks(db: Session, employee_id: int, run: PayRun):
    q = (
        select(Paycheck, PayRun).join(PayRun)
        .where(Paycheck.employee_id == employee_id, PayRun.status == "final", PayRun.id != run.id,
               PayRun.pay_date >= dt.date(run.pay_date.year, 1, 1), PayRun.pay_date <= run.pay_date)
    )
    return [(p, r) for p, r in db.execute(q) if not (r.pay_date == run.pay_date and r.id > run.id)]


