"""Payroll calculation for a pay run."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PAY_FREQUENCIES, Company, Employee, Paycheck, PayRun
from .money import round_cents
from .tax.tables import params_for
from .tax.withholding import W4, federal_withholding


@dataclass
class YTD:
    gross: int = 0
    ss_wages: int = 0
    medicare_wages: int = 0
    futa_wages: int = 0
    suta_wages: int = 0


def ytd_before(db: Session, employee_id: int, pay_run: PayRun) -> YTD:
    """Totals from finalized pay runs in the same calendar year that come before `pay_run`."""
    start = dt.date(pay_run.pay_date.year, 1, 1)
    q = (
        select(Paycheck, PayRun)
        .join(PayRun)
        .where(
            Paycheck.employee_id == employee_id,
            PayRun.status == "final",
            PayRun.pay_date >= start,
            PayRun.pay_date <= pay_run.pay_date,
            PayRun.id != pay_run.id,
        )
    )
    ytd = YTD()
    for pc, run in db.execute(q):
        if run.pay_date == pay_run.pay_date and pay_run.id is not None and run.id > pay_run.id:
            continue
        ytd.gross += pc.gross
        ytd.ss_wages += pc.ss_wages
        ytd.medicare_wages += pc.medicare_wages
        ytd.futa_wages += pc.futa_wages
        ytd.suta_wages += pc.suta_wages
    return ytd


def employee_w4(emp: Employee) -> W4:
    return W4(
        filing_status=emp.filing_status,
        step2_checked=emp.w4_step2,
        step3_credits=emp.w4_step3,
        step4a_other_income=emp.w4_step4a,
        step4b_deductions=emp.w4_step4b,
        step4c_extra=emp.w4_step4c,
        exempt=emp.w4_exempt,
    )


def _capped(wages: int, ytd: int, base: int) -> int:
    """Portion of `wages` that falls under an annual wage base (0 base = unlimited)."""
    if base <= 0:
        return wages
    return max(0, min(wages, base - ytd))


def compute_paycheck(db: Session, company: Company, pc: Paycheck, run: PayRun) -> Paycheck:
    """Fill in all tax fields of `pc` from its gross and shareholder_health.

    2% shareholder health premiums are income-tax wages (W-2 box 1, Form 941
    line 2) but are not Social Security, Medicare or FUTA wages.
    """
    p = params_for(run.pay_date.year)
    emp = pc.employee if pc.employee is not None else db.get(Employee, pc.employee_id)
    ytd = ytd_before(db, emp.id, run)
    periods = PAY_FREQUENCIES[company.pay_frequency]

    pc.fit_wages = pc.gross + pc.shareholder_health
    pc.ss_wages = _capped(pc.gross, ytd.ss_wages, p.ss_wage_base)
    pc.medicare_wages = pc.gross
    pc.addl_medicare_wages = max(
        0, ytd.medicare_wages + pc.gross - max(p.addl_medicare_threshold, ytd.medicare_wages)
    )
    pc.futa_wages = _capped(pc.gross, ytd.futa_wages, p.futa_wage_base)
    pc.suta_wages = 0 if emp.suta_exempt else _capped(pc.gross, ytd.suta_wages, company.suta_wage_base)

    if not pc.manual:
        pc.fit = federal_withholding(pc.fit_wages, periods, employee_w4(emp), run.pay_date.year)
        pc.ss_ee = round_cents(pc.ss_wages * p.ss_rate)
        pc.medicare_ee = round_cents(pc.medicare_wages * p.medicare_rate)
        pc.addl_medicare = round_cents(pc.addl_medicare_wages * p.addl_medicare_rate)
    pc.ss_er = round_cents(pc.ss_wages * p.ss_rate)
    pc.medicare_er = round_cents(pc.medicare_wages * p.medicare_rate)
    pc.futa = round_cents(pc.futa_wages * p.futa_rate)
    suta_rate = Decimal(company.suta_rate_pct or "0") / 100
    pc.suta = round_cents(pc.suta_wages * suta_rate)
    return pc


def salary_per_period(company: Company, emp: Employee) -> int:
    return round_cents(Decimal(emp.annual_salary) / PAY_FREQUENCIES[company.pay_frequency])


def recompute_run(db: Session, company: Company, run: PayRun) -> None:
    for pc in run.paychecks:
        compute_paycheck(db, company, pc, run)


def later_final_runs(db: Session, run: PayRun) -> list[PayRun]:
    """Finalized runs in the same year dated after `run` (their YTD would be affected)."""
    return list(
        db.scalars(
            select(PayRun).where(
                PayRun.status == "final",
                PayRun.pay_date > run.pay_date,
                PayRun.pay_date <= dt.date(run.pay_date.year, 12, 31),
            )
        )
    )
