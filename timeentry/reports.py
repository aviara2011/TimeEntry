"""Federal form worksheets built from finalized payroll, invoices and expenses.

These produce the numbers to transcribe onto the IRS/SSA forms. They do not
e-file anything.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import EXPENSE_CATEGORIES, Company, Employee, Expense, Invoice, Payment, Paycheck, PayRun, TaxDeposit
from .money import round_cents
from .tax.tables import params_for


def quarter_bounds(year: int, quarter: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, 3 * quarter - 2, 1)
    end = dt.date(year + (quarter == 4), (3 * quarter) % 12 + 1, 1) - dt.timedelta(days=1)
    return start, end


def quarter_of(d: dt.date) -> int:
    return (d.month - 1) // 3 + 1


def final_paychecks(db: Session, start: dt.date, end: dt.date) -> list[tuple[Paycheck, PayRun]]:
    q = (
        select(Paycheck, PayRun)
        .join(PayRun)
        .where(PayRun.status == "final", PayRun.pay_date >= start, PayRun.pay_date <= end)
        .order_by(PayRun.pay_date, PayRun.id)
    )
    return [(pc, run) for pc, run in db.execute(q)]


def _due_after_quarter(year: int, quarter: int) -> dt.date:
    """Last day of the month following the quarter (941 due date, before weekend adjustment)."""
    _, end = quarter_bounds(year, quarter)
    nxt = end + dt.timedelta(days=1)
    return dt.date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1) - dt.timedelta(days=1)


# ---------------------------------------------------------------- Form 941
@dataclass
class Form941:
    year: int
    quarter: int
    line1_employees: int = 0
    line2_wages: int = 0
    line3_fit: int = 0
    line5a_ss_wages: int = 0
    line5a_tax: int = 0
    line5c_medicare_wages: int = 0
    line5c_tax: int = 0
    line5d_addl_wages: int = 0
    line5d_tax: int = 0
    line5e: int = 0
    line6: int = 0
    line7_fractions: int = 0
    line10: int = 0
    line12: int = 0
    line13_deposits: int = 0
    line14_balance_due: int = 0
    line15_overpayment: int = 0
    month_liability: list[int] = field(default_factory=lambda: [0, 0, 0])
    due_date: dt.date | None = None
    no_wages: bool = True

    @property
    def months(self) -> list[str]:
        return [dt.date(self.year, 3 * self.quarter - 2 + i, 1).strftime("%B") for i in range(3)]


def form_941(db: Session, year: int, quarter: int) -> Form941:
    p = params_for(year)
    start, end = quarter_bounds(year, quarter)
    f = Form941(year=year, quarter=quarter, due_date=_due_after_quarter(year, quarter))
    rows = final_paychecks(db, start, end)
    twelfth = dt.date(year, 3 * quarter, 12)
    covering = {pc.employee_id for pc, run in rows if run.period_start <= twelfth <= run.period_end}
    f.line1_employees = len(covering)
    actual = 0
    for pc, run in rows:
        f.no_wages = False
        f.line2_wages += pc.fit_wages
        f.line3_fit += pc.fit
        f.line5a_ss_wages += pc.ss_wages
        f.line5c_medicare_wages += pc.medicare_wages
        f.line5d_addl_wages += pc.addl_medicare_wages
        actual += pc.liability_941
        f.month_liability[run.pay_date.month - start.month] += pc.liability_941
    f.line5a_tax = round_cents(f.line5a_ss_wages * p.ss_rate * 2)
    f.line5c_tax = round_cents(f.line5c_medicare_wages * p.medicare_rate * 2)
    f.line5d_tax = round_cents(f.line5d_addl_wages * p.addl_medicare_rate)
    f.line5e = f.line5a_tax + f.line5c_tax + f.line5d_tax
    f.line6 = f.line3_fit + f.line5e
    f.line7_fractions = actual - f.line6
    f.line10 = f.line6 + f.line7_fractions
    f.line12 = f.line10
    f.line13_deposits = sum(
        d.amount
        for d in db.scalars(
            select(TaxDeposit).where(TaxDeposit.form == "941", TaxDeposit.year == year, TaxDeposit.quarter == quarter)
        )
    )
    diff = f.line12 - f.line13_deposits
    f.line14_balance_due = max(0, diff)
    f.line15_overpayment = max(0, -diff)
    return f


# ---------------------------------------------------------------- Form 940
@dataclass
class Form940:
    year: int
    line3_total_payments: int = 0
    line4_exempt: int = 0
    line5_over_base: int = 0
    line6: int = 0
    line7_futa_wages: int = 0
    line8_futa_tax: int = 0
    line12: int = 0
    line13_deposits: int = 0
    line14_balance_due: int = 0
    line15_overpayment: int = 0
    quarter_liability: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    due_date: dt.date | None = None


def form_940(db: Session, year: int) -> Form940:
    p = params_for(year)
    f = Form940(year=year, due_date=dt.date(year + 1, 1, 31))
    per_emp: dict[int, int] = defaultdict(int)
    for pc, run in final_paychecks(db, dt.date(year, 1, 1), dt.date(year, 12, 31)):
        f.line3_total_payments += pc.gross + pc.shareholder_health
        f.line4_exempt += pc.shareholder_health
        per_emp[pc.employee_id] += pc.gross
        f.quarter_liability[quarter_of(run.pay_date) - 1] += pc.futa
    f.line5_over_base = sum(max(0, g - p.futa_wage_base) for g in per_emp.values())
    f.line6 = f.line4_exempt + f.line5_over_base
    f.line7_futa_wages = f.line3_total_payments - f.line6
    f.line8_futa_tax = round_cents(f.line7_futa_wages * p.futa_rate)
    f.line12 = f.line8_futa_tax
    f.line13_deposits = sum(
        d.amount for d in db.scalars(select(TaxDeposit).where(TaxDeposit.form == "940", TaxDeposit.year == year))
    )
    diff = f.line12 - f.line13_deposits
    f.line14_balance_due = max(0, diff)
    f.line15_overpayment = max(0, -diff)
    return f


# ---------------------------------------------------------------- W-2 / W-3
@dataclass
class W2:
    employee: Employee
    box1: int = 0
    box2: int = 0
    box3: int = 0
    box4: int = 0
    box5: int = 0
    box6: int = 0
    box14_health: int = 0
    suta_wages: int = 0
    gross: int = 0


def w2s(db: Session, year: int) -> list[W2]:
    out: dict[int, W2] = {}
    for pc, _ in final_paychecks(db, dt.date(year, 1, 1), dt.date(year, 12, 31)):
        w = out.setdefault(pc.employee_id, W2(employee=pc.employee))
        w.box1 += pc.fit_wages
        w.box2 += pc.fit
        w.box3 += pc.ss_wages
        w.box4 += pc.ss_ee
        w.box5 += pc.medicare_wages
        w.box6 += pc.medicare_ee + pc.addl_medicare
        w.box14_health += pc.shareholder_health
        w.suta_wages += pc.suta_wages
        w.gross += pc.gross
    return sorted(out.values(), key=lambda w: (w.employee.last_name, w.employee.first_name))


def w3_totals(forms: list[W2]) -> dict[str, int]:
    keys = ["box1", "box2", "box3", "box4", "box5", "box6", "box14_health"]
    return {k: sum(getattr(w, k) for w in forms) for k in keys}


# ---------------------------------------------------------------- Form 1120-S
@dataclass
class K1:
    shareholder: Employee
    pct: Decimal
    ordinary_income: int
    distributions: int
    nondeductible: int


@dataclass
class Form1120S:
    year: int
    method: str
    line1a_gross_receipts: int = 0
    line2_cogs: int = 0
    line3_gross_profit: int = 0
    line6_total_income: int = 0
    lines: dict[str, int] = field(default_factory=dict)  # deduction line -> amount
    other_deductions: dict[str, int] = field(default_factory=dict)  # category label -> amount (line 19 detail)
    line20_total_deductions: int = 0
    line21_ordinary_income: int = 0
    officer_comp: list[tuple[Employee, int, Decimal]] = field(default_factory=list)  # Form 1125-E
    k1s: list[K1] = field(default_factory=list)
    distributions_total: int = 0
    nondeductible_total: int = 0
    ownership_total: Decimal = Decimal(0)
    warnings: list[str] = field(default_factory=list)


LINE_LABELS_1120S = {
    "7": "Compensation of officers (Form 1125-E)",
    "8": "Salaries and wages (less employment credits)",
    "9": "Repairs and maintenance",
    "10": "Bad debts",
    "11": "Rents",
    "12": "Taxes and licenses",
    "13": "Interest",
    "14": "Depreciation",
    "16": "Advertising",
    "17": "Pension, profit-sharing, etc., plans",
    "18": "Employee benefit programs",
    "19": "Other deductions (attach statement)",
}


def form_1120s(db: Session, company: Company, year: int) -> Form1120S:
    start, end = dt.date(year, 1, 1), dt.date(year, 12, 31)
    f = Form1120S(year=year, method=company.accounting_method)
    if company.accounting_method == "accrual":
        invoices = db.scalars(
            select(Invoice).where(Invoice.issue_date >= start, Invoice.issue_date <= end, Invoice.status != "void")
        )
        f.line1a_gross_receipts = sum(i.total for i in invoices)
    else:
        f.line1a_gross_receipts = sum(
            p.amount for p in db.scalars(select(Payment).where(Payment.date >= start, Payment.date <= end))
        )

    lines: dict[str, int] = defaultdict(int)
    officer: dict[int, int] = defaultdict(int)
    for pc, _ in final_paychecks(db, start, end):
        if pc.employee.is_officer:
            lines["7"] += pc.fit_wages
            officer[pc.employee_id] += pc.fit_wages
        else:
            lines["8"] += pc.fit_wages
        lines["12"] += pc.employer_taxes

    per_holder_dist: dict[int, int] = defaultdict(int)
    per_holder_nd: dict[int, int] = defaultdict(int)
    for e in db.scalars(select(Expense).where(Expense.date >= start, Expense.date <= end)):
        label, line = EXPENSE_CATEGORIES.get(e.category, EXPENSE_CATEGORIES["other"])
        if line == "2":
            f.line2_cogs += e.amount
        elif line == "K-16d":
            f.distributions_total += e.amount
            if e.shareholder_id:
                per_holder_dist[e.shareholder_id] += e.amount
        elif line == "K-16c":
            f.nondeductible_total += e.amount
            if e.shareholder_id:
                per_holder_nd[e.shareholder_id] += e.amount
        else:
            amount = e.amount
            if e.category == "meals":
                amount = round_cents(Decimal(amount) / 2)
            lines[line] += amount
            if line == "19":
                f.other_deductions[label] = f.other_deductions.get(label, 0) + amount

    f.line3_gross_profit = f.line1a_gross_receipts - f.line2_cogs
    f.line6_total_income = f.line3_gross_profit
    f.lines = {k: lines[k] for k in LINE_LABELS_1120S if lines.get(k)}
    f.line20_total_deductions = sum(f.lines.values())
    f.line21_ordinary_income = f.line6_total_income - f.line20_total_deductions

    holders = [e for e in db.scalars(select(Employee)) if Decimal(e.ownership_pct or "0") > 0]
    f.ownership_total = sum((Decimal(e.ownership_pct) for e in holders), Decimal(0))
    for e in holders:
        pct = Decimal(e.ownership_pct)
        f.k1s.append(
            K1(
                shareholder=e,
                pct=pct,
                ordinary_income=round_cents(Decimal(f.line21_ordinary_income) * pct / 100),
                distributions=per_holder_dist.get(e.id, 0),
                nondeductible=per_holder_nd.get(e.id, 0),
            )
        )
    for emp_id, amount in officer.items():
        emp = db.get(Employee, emp_id)
        f.officer_comp.append((emp, amount, Decimal(emp.ownership_pct or "0")))

    if holders and f.ownership_total != 100:
        f.warnings.append(f"Shareholder ownership adds up to {f.ownership_total}%, not 100%.")
    if f.distributions_total and not lines.get("7"):
        f.warnings.append(
            "Distributions were paid but no officer compensation was recorded. The IRS expects "
            "S-Corp shareholder-employees to take reasonable compensation through payroll."
        )
    unassigned = f.distributions_total - sum(per_holder_dist.values())
    if unassigned:
        f.warnings.append("Some distributions are not assigned to a shareholder.")
    if len(holders) > 1 and per_holder_dist:
        ratios = {e.id: Decimal(per_holder_dist.get(e.id, 0)) / Decimal(e.ownership_pct) for e in holders}
        if len({round(r) for r in ratios.values()}) > 1:
            f.warnings.append(
                "Distributions are not proportional to ownership. An S-Corp may have only one class of "
                "stock, so disproportionate distributions should be corrected — ask your CPA."
            )
    return f


# ---------------------------------------------------------------- deadlines
def upcoming_deadlines(today: dt.date, company: Company) -> list[tuple[dt.date, str]]:
    items: list[tuple[dt.date, str]] = []
    for year in (today.year, today.year + 1):
        for q in range(1, 5):
            items.append((_due_after_quarter(year, q), f"Form 941 for Q{q} {year}"))
        items.append((dt.date(year, 1, 31), f"Form 940, W-2s to employees, W-2/W-3 to SSA for {year - 1}"))
        items.append((dt.date(year, 3, 15), f"Form 1120-S and K-1s for {year - 1} (or file Form 7004)"))
        for month in range(1, 13):
            items.append((dt.date(year, month, 15), "Monthly 941 deposit (EFTPS) for last month's payroll"))
        if company.state == "WY":
            for q in range(1, 5):
                items.append((_due_after_quarter(year, q), f"Wyoming UI / workers' comp quarterly report Q{q} {year}"))
    return sorted((d, label) for d, label in items if today <= d <= today + dt.timedelta(days=75))
