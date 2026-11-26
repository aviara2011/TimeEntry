from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

PAY_FREQUENCIES = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}


class Company(Base):
    __tablename__ = "company"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="My Company, Inc.")
    address: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    ein: Mapped[str] = mapped_column(String(20), default="")
    state: Mapped[str] = mapped_column(String(2), default="WY")
    state_account_no: Mapped[str] = mapped_column(String(50), default="")
    pay_frequency: Mapped[str] = mapped_column(String(20), default="monthly")
    # State unemployment: rate in percent as text, e.g. "1.0"
    suta_rate_pct: Mapped[str] = mapped_column(String(20), default="0")
    suta_wage_base: Mapped[int] = mapped_column(Integer, default=0)  # cents; 0 = no limit
    accounting_method: Mapped[str] = mapped_column(String(10), default="cash")  # cash | accrual
    invoice_prefix: Mapped[str] = mapped_column(String(20), default="INV-")
    next_invoice_number: Mapped[int] = mapped_column(Integer, default=1001)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    invoice_footer: Mapped[str] = mapped_column(Text, default="Thank you for your business.")
    incorporation_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class Client(Base):
    __tablename__ = "client"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    contact: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    default_rate: Mapped[int] = mapped_column(Integer, default=0)  # cents per hour
    terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    projects: Mapped[list[Project]] = relationship(back_populates="client", order_by="Project.name")


class Project(Base):
    __tablename__ = "project"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    name: Mapped[str] = mapped_column(String(200))
    rate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # overrides client rate
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    client: Mapped[Client] = relationship(back_populates="projects")

    @property
    def effective_rate(self) -> int:
        return self.rate if self.rate is not None else self.client.default_rate

    @property
    def label(self) -> str:
        return f"{self.client.name} — {self.name}"


class Employee(Base):
    __tablename__ = "employee"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    ssn: Mapped[str] = mapped_column(String(11), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    is_officer: Mapped[bool] = mapped_column(Boolean, default=True)
    ownership_pct: Mapped[str] = mapped_column(String(20), default="0")  # e.g. "50"
    annual_salary: Mapped[int] = mapped_column(Integer, default=0)  # cents
    # Form W-4 (2020 or later)
    filing_status: Mapped[str] = mapped_column(String(10), default="single")
    w4_step2: Mapped[bool] = mapped_column(Boolean, default=False)
    w4_step3: Mapped[int] = mapped_column(Integer, default=0)
    w4_step4a: Mapped[int] = mapped_column(Integer, default=0)
    w4_step4b: Mapped[int] = mapped_column(Integer, default=0)
    w4_step4c: Mapped[int] = mapped_column(Integer, default=0)
    w4_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    suta_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def ssn_masked(self) -> str:
        digits = "".join(c for c in self.ssn if c.isdigit())
        return f"XXX-XX-{digits[-4:]}" if len(digits) >= 4 else ""


class TimeEntry(Base):
    __tablename__ = "time_entry"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"), nullable=True)
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    billable: Mapped[bool] = mapped_column(Boolean, default=True)
    timer_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoice.id"), nullable=True)
    project: Mapped[Project] = relationship()
    employee: Mapped[Employee | None] = relationship()


class Invoice(Base):
    __tablename__ = "invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(50), unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    issue_date: Mapped[dt.date] = mapped_column(Date)
    due_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft|sent|paid|void
    notes: Mapped[str] = mapped_column(Text, default="")
    client: Mapped[Client] = relationship()
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.position"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="Payment.date"
    )

    @property
    def total(self) -> int:
        return sum(line.amount for line in self.lines)

    @property
    def paid(self) -> int:
        return sum(p.amount for p in self.payments)

    @property
    def balance(self) -> int:
        return 0 if self.status == "void" else self.total - self.paid


class InvoiceLine(Base):
    __tablename__ = "invoice_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    time_entry_id: Mapped[int | None] = mapped_column(ForeignKey("time_entry.id"), nullable=True)
    date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[str] = mapped_column(String(20), default="1")  # hours or units, decimal text
    unit_price: Mapped[int] = mapped_column(Integer, default=0)  # cents
    amount: Mapped[int] = mapped_column(Integer, default=0)  # cents
    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Payment(Base):
    __tablename__ = "payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice.id"))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(50), default="")
    memo: Mapped[str] = mapped_column(Text, default="")
    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class PayRun(Base):
    __tablename__ = "pay_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    pay_date: Mapped[dt.date] = mapped_column(Date, index=True)
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | final
    memo: Mapped[str] = mapped_column(Text, default="")
    paychecks: Mapped[list[Paycheck]] = relationship(
        back_populates="pay_run", cascade="all, delete-orphan", order_by="Paycheck.employee_id"
    )


class Paycheck(Base):
    __tablename__ = "paycheck"
    id: Mapped[int] = mapped_column(primary_key=True)
    pay_run_id: Mapped[int] = mapped_column(ForeignKey("pay_run.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"))
    gross: Mapped[int] = mapped_column(Integer, default=0)            # cash wages
    shareholder_health: Mapped[int] = mapped_column(Integer, default=0)  # 2% shareholder health premiums
    fit_wages: Mapped[int] = mapped_column(Integer, default=0)
    fit: Mapped[int] = mapped_column(Integer, default=0)
    ss_wages: Mapped[int] = mapped_column(Integer, default=0)
    ss_ee: Mapped[int] = mapped_column(Integer, default=0)
    ss_er: Mapped[int] = mapped_column(Integer, default=0)
    medicare_wages: Mapped[int] = mapped_column(Integer, default=0)
    medicare_ee: Mapped[int] = mapped_column(Integer, default=0)
    medicare_er: Mapped[int] = mapped_column(Integer, default=0)
    addl_medicare_wages: Mapped[int] = mapped_column(Integer, default=0)
    addl_medicare: Mapped[int] = mapped_column(Integer, default=0)
    futa_wages: Mapped[int] = mapped_column(Integer, default=0)
    futa: Mapped[int] = mapped_column(Integer, default=0)
    suta_wages: Mapped[int] = mapped_column(Integer, default=0)
    suta: Mapped[int] = mapped_column(Integer, default=0)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)  # taxes entered by hand
    pay_run: Mapped[PayRun] = relationship(back_populates="paychecks")
    employee: Mapped[Employee] = relationship()

    @property
    def employee_taxes(self) -> int:
        return self.fit + self.ss_ee + self.medicare_ee + self.addl_medicare

    @property
    def net(self) -> int:
        return self.gross - self.employee_taxes

    @property
    def employer_taxes(self) -> int:
        return self.ss_er + self.medicare_er + self.futa + self.suta

    @property
    def liability_941(self) -> int:
        return self.fit + self.ss_ee + self.ss_er + self.medicare_ee + self.medicare_er + self.addl_medicare


EXPENSE_CATEGORIES: dict[str, tuple[str, str]] = {
    # key: (label, Form 1120-S line)
    "rent": ("Rent", "11"),
    "taxes_licenses": ("Taxes and licenses (non-payroll)", "12"),
    "interest": ("Interest expense", "13"),
    "depreciation": ("Depreciation (from Form 4562)", "14"),
    "advertising": ("Advertising", "16"),
    "retirement": ("Retirement plan contributions (employer)", "17"),
    "benefits": ("Employee benefit programs", "18"),
    "repairs": ("Repairs and maintenance", "9"),
    "bad_debts": ("Bad debts", "10"),
    "software": ("Software & subscriptions", "19"),
    "professional": ("Legal & professional fees", "19"),
    "office": ("Office supplies & expenses", "19"),
    "phone_internet": ("Phone & internet", "19"),
    "insurance": ("Insurance (business)", "19"),
    "travel": ("Travel", "19"),
    "meals": ("Business meals (50% deductible)", "19"),
    "bank_fees": ("Bank & merchant fees", "19"),
    "contractors": ("Contract labor (1099)", "19"),
    "other": ("Other deductions", "19"),
    "cogs": ("Cost of goods sold", "2"),
    "distribution": ("Shareholder distribution (not deductible)", "K-16d"),
    "nondeductible": ("Non-deductible (fines, personal, etc.)", "K-16c"),
}


class Expense(Base):
    __tablename__ = "expense"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    payee: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(30), default="other")
    amount: Mapped[int] = mapped_column(Integer)
    memo: Mapped[str] = mapped_column(Text, default="")
    shareholder_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"), nullable=True)
    shareholder: Mapped[Employee | None] = relationship()


class TaxDeposit(Base):
    """A federal tax payment made through EFTPS (Form 941 or 940 liability)."""

    __tablename__ = "tax_deposit"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    form: Mapped[str] = mapped_column(String(5))  # "941" | "940"
    year: Mapped[int] = mapped_column(Integer)
    quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[int] = mapped_column(Integer)
    confirmation: Mapped[str] = mapped_column(String(100), default="")
