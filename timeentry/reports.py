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

from .models import Company, Employee, Expense, Paycheck, PayRun, TaxDeposit
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
    interest: int = 0
    charity: int = 0


@dataclass
class Form1120S:
    year: int
    method: str
    line1a_gross_receipts: int = 0
    line1b_returns: int = 0
    line1c: int = 0
    line2_cogs: int = 0
    line3_gross_profit: int = 0
    line5_other_income: int = 0
    line6_total_income: int = 0
    lines: dict[str, int] = field(default_factory=dict)  # deduction line -> amount
    line_detail: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # line -> [(account, amount)]
    line20_total_deductions: int = 0
    line21_ordinary_income: int = 0
    k4_interest: int = 0
    k12a_charity: int = 0
    nondeductible_total: int = 0
    distributions_total: int = 0
    officer_comp: list[tuple[Employee, int, Decimal]] = field(default_factory=list)  # Form 1125-E
    k1s: list[K1] = field(default_factory=list)
    ownership_total: Decimal = Decimal(0)
    schedule_l: list[tuple[str, str, int, int]] = field(default_factory=list)  # (line, label, begin, end)
    unmapped: list[tuple[str, int]] = field(default_factory=list)
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

SCHEDULE_L = [
    ("L1", "Cash"), ("L2a", "Trade notes and accounts receivable"), ("L6", "Other current assets"),
    ("L10a", "Buildings and other depreciable assets"), ("L10b", "Less accumulated depreciation"),
    ("L15", "Total assets"),
    ("L16", "Accounts payable"), ("L18", "Other current liabilities"), ("L19", "Loans from shareholders"),
    ("L20", "Mortgages, notes, bonds payable in 1 year or more"), ("L22", "Capital stock"),
    ("L23", "Additional paid-in capital"), ("L24", "Retained earnings"), ("L27", "Total liabilities and shareholders' equity"),
]


def _schedule_l(books, as_of: dt.date) -> dict[str, int]:
    from .ledger import balance_sheet

    bs = balance_sheet(books, as_of)
    out: dict[str, int] = defaultdict(int)
    for a, amt in bs["assets"].rows:
        out[a.tax_line if a.tax_line in ("L1", "L2a", "L6", "L10a", "L10b") else "L6"] += amt
    for a, amt in bs["liabilities"].rows:
        out[a.tax_line if a.tax_line in ("L16", "L18", "L19", "L20") else "L18"] += amt
    for a, amt in bs["equity"].rows:
        out[a.tax_line if a.tax_line in ("L22", "L23") else "L24"] += amt
    out["L24"] += bs["net_income"]
    out["L15"] = bs["assets"].total
    out["L27"] = bs["total_le"]
    return out


def form_1120s(db: Session, company: Company, year: int) -> Form1120S:
    from .ledger import Books

    start, end = dt.date(year, 1, 1), dt.date(year, 12, 31)
    books = Books(db, company.accounting_method)
    f = Form1120S(year=year, method=company.accounting_method)
    bal = books.balances(start, end)
    lines: dict[str, int] = defaultdict(int)
    detail: dict[str, list] = defaultdict(list)
    for acct in sorted(books.accounts.values(), key=lambda a: a.number):
        amt = bal.get(acct.id, 0)
        if not amt or acct.type in ("asset", "liability"):
            continue
        tl = acct.tax_line
        if acct.type == "income":
            amt = -amt
            if tl == "1a":
                f.line1a_gross_receipts += amt
            elif tl == "1b":
                f.line1b_returns += -amt  # contra-revenue: debit balance shown as a positive reduction
            elif tl == "5":
                f.line5_other_income += amt
            elif tl == "K4":
                f.k4_interest += amt
            else:
                f.unmapped.append((acct.label, amt))
        elif acct.type == "expense":
            deductible = round(amt * acct.deductible_pct / 100)
            f.nondeductible_total += amt - deductible
            if tl == "2":
                f.line2_cogs += deductible
            elif tl in LINE_LABELS_1120S:
                lines[tl] += deductible
                detail[tl].append((acct.label + (f" ({acct.deductible_pct}% deductible)" if acct.deductible_pct != 100 else ""), deductible))
            elif tl == "K12a":
                f.k12a_charity += amt
            elif tl == "K16c":
                f.nondeductible_total += deductible
            else:
                f.unmapped.append((acct.label, amt))
        elif acct.type == "equity" and acct.code == "distribution":
            f.distributions_total += amt
    f.line1c = f.line1a_gross_receipts - f.line1b_returns
    f.line3_gross_profit = f.line1c - f.line2_cogs
    f.line6_total_income = f.line3_gross_profit + f.line5_other_income
    f.lines = {k: lines[k] for k in LINE_LABELS_1120S if lines.get(k)}
    f.line_detail = {k: detail[k] for k in f.lines}
    f.line20_total_deductions = sum(f.lines.values())
    f.line21_ordinary_income = f.line6_total_income - f.line20_total_deductions

    # Officer compensation per officer (Form 1125-E): W-2 box 1 wages from payroll
    officer: dict[int, int] = defaultdict(int)
    for pc, _ in final_paychecks(db, start, end):
        if pc.employee.is_officer:
            officer[pc.employee_id] += pc.fit_wages
    for emp_id, amount in officer.items():
        emp = db.get(Employee, emp_id)
        f.officer_comp.append((emp, amount, Decimal(emp.ownership_pct or "0")))

    # Distributions per shareholder
    dist_id = books.code["distribution"]
    per_holder: dict[int, int] = defaultdict(int)
    for e in db.scalars(select(Expense).where(Expense.date >= start, Expense.date <= end, Expense.account_id == dist_id)):
        per_holder[e.shareholder_id or 0] += e.amount

    holders = [e for e in db.scalars(select(Employee)) if Decimal(e.ownership_pct or "0") > 0]
    f.ownership_total = sum((Decimal(e.ownership_pct) for e in holders), Decimal(0))

    def share(total: int, pct: Decimal) -> int:
        return round_cents(Decimal(total) * pct / 100)

    for e in holders:
        pct = Decimal(e.ownership_pct)
        f.k1s.append(K1(shareholder=e, pct=pct, ordinary_income=share(f.line21_ordinary_income, pct),
                        distributions=per_holder.get(e.id, 0), nondeductible=share(f.nondeductible_total, pct),
                        interest=share(f.k4_interest, pct), charity=share(f.k12a_charity, pct)))

    begin_l = _schedule_l(books, start - dt.timedelta(days=1))
    end_l = _schedule_l(books, end)
    f.schedule_l = [(line, label, begin_l.get(line, 0), end_l.get(line, 0)) for line, label in SCHEDULE_L]

    if holders and f.ownership_total != 100:
        f.warnings.append(f"Shareholder ownership adds up to {f.ownership_total}%, not 100%.")
    if f.distributions_total and not lines.get("7"):
        f.warnings.append(
            "Distributions were paid but no officer compensation was recorded. The IRS expects "
            "S-Corp shareholder-employees to take reasonable compensation through payroll."
        )
    if per_holder.get(0):
        f.warnings.append("Some distributions are not assigned to a shareholder (edit them under Expenses).")
    if len(holders) > 1 and any(per_holder.get(e.id) for e in holders):
        ratios = {round(Decimal(per_holder.get(e.id, 0)) / Decimal(e.ownership_pct)) for e in holders}
        if len(ratios) > 1:
            f.warnings.append(
                "Distributions are not proportional to ownership. An S-Corp may have only one class of "
                "stock, so disproportionate distributions should be corrected. Ask your CPA."
            )
    if f.unmapped:
        f.warnings.append("Some income/expense accounts have no tax line; set one under Books → Chart of accounts.")
    if end_l.get("L15") != end_l.get("L27"):
        f.warnings.append("The balance sheet doesn't balance. Check Books → Trial balance.")
    opening = books.balances(None, end).get(books.code["opening"], 0)
    if opening:
        f.warnings.append("Opening balance equity isn't zero. Ask your CPA for the journal entry that moves it "
                          "into retained earnings / capital stock.")
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
