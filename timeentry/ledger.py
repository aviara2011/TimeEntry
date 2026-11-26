"""General ledger.

The ledger is *derived*: every invoice, payment, expense, paycheck, tax deposit,
categorized bank transaction and manual journal entry is turned into balanced
postings when a report is run. Nothing is double-entered by hand, and fixing a
source record fixes the books.

Amounts are signed cents: positive = debit, negative = credit.

Basis:
  accrual: invoices post to Accounts receivable when sent; payments clear A/R.
  cash:    invoices don't post; payments post straight to the invoice's income accounts.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Account, BankAccount, BankTransaction, Expense, Invoice, JournalEntry, Payment, PayRun, TaxDeposit,
)

EPOCH = dt.date(1900, 1, 1)


@dataclass
class Posting:
    date: dt.date
    account_id: int
    amount: int
    source: str        # invoice | payment | expense | payroll | tax_deposit | bank | journal | opening
    source_id: int
    memo: str = ""
    url: str = ""


class Books:
    """All postings for one basis, plus helpers for balances and reports."""

    def __init__(self, db: Session, basis: str = "accrual"):
        self.db = db
        self.basis = basis
        self.accounts: dict[int, Account] = {a.id: a for a in db.scalars(select(Account))}
        self.code = {a.code: a.id for a in self.accounts.values() if a.code}
        self.postings: list[Posting] = []
        self._build()

    # ---------------------------------------------------------- generation
    def _add(self, entries: list[tuple[int, int]], date, source, source_id, memo="", url=""):
        entries = [(a, amt) for a, amt in entries if amt]
        assert sum(amt for _, amt in entries) == 0, f"unbalanced {source} {source_id}: {entries}"
        for acct, amt in entries:
            self.postings.append(Posting(date, acct, amt, source, source_id, memo, url))

    def _build(self):
        db, c = self.db, self.code
        banks = {b.id: b for b in db.scalars(select(BankAccount))}
        bank_gl = {b.id: b.gl_account_id for b in banks.values() if b.gl_account_id}
        default_cash = next((bank_gl[b.id] for b in sorted(banks.values(), key=lambda b: b.id)
                             if b.active and b.id in bank_gl), c["undeposited"])

        # Which bank account each record's money moved through (from bank matching).
        via: dict[tuple[str, int], int] = {}
        txns = db.scalars(select(BankTransaction)).all()
        for t in txns:
            gl = bank_gl.get(t.account_id)
            if not gl:
                continue
            for kind, rid in (("payment", t.payment_id), ("expense", t.expense_id), ("taxdep", t.tax_deposit_id),
                              ("paycheck", t.paycheck_id), ("payrun", t.pay_run_id)):
                if rid:
                    via[(kind, rid)] = gl

        # Opening balances of bank accounts
        first_txn: dict[int, dt.date] = {}
        for t in txns:
            if t.account_id not in first_txn or t.date < first_txn[t.account_id]:
                first_txn[t.account_id] = t.date
        for b in banks.values():
            if b.opening_balance and b.id in bank_gl:
                d = b.opening_date or (first_txn.get(b.id, dt.date.today()) - dt.timedelta(days=1))
                self._add([(bank_gl[b.id], b.opening_balance), (c["opening"], -b.opening_balance)],
                          d, "opening", b.id, f"Opening balance {b.label}", f"/bank/{b.id}")

        # Invoices and payments
        def income_split(inv: Invoice, amount: int) -> list[tuple[int, int]]:
            """Allocate `amount` to the invoice's income accounts in proportion to its lines."""
            by_acct: dict[int, int] = defaultdict(int)
            for line in inv.lines:
                by_acct[line.account_id or c["revenue"]] += line.amount
            total = sum(by_acct.values())
            if not total:
                return [(c["revenue"], amount)]
            out, left = [], amount
            items = list(by_acct.items())
            for i, (acct, amt) in enumerate(items):
                share = left if i == len(items) - 1 else round(amount * amt / total)
                out.append((acct, share))
                left -= share
            return out

        for inv in db.scalars(select(Invoice).where(Invoice.status.in_(["sent", "paid"]))):
            url = f"/invoices/{inv.id}"
            if self.basis == "accrual":
                self._add([(c["ar"], inv.total)] + [(a, -amt) for a, amt in income_split(inv, inv.total)],
                          inv.issue_date, "invoice", inv.id, f"Invoice {inv.number} {inv.client.name}", url)
            for p in inv.payments:
                cash = via.get(("payment", p.id), c["undeposited"])
                credit = [(c["ar"], -p.amount)] if self.basis == "accrual" else \
                    [(a, -amt) for a, amt in income_split(inv, p.amount)]
                self._add([(cash, p.amount)] + credit, p.date, "payment", p.id,
                          f"Payment {inv.number} {inv.client.name}", url)

        # Money out recorded as expenses / distributions / other spending
        for e in db.scalars(select(Expense)):
            cash = via.get(("expense", e.id)) or (bank_gl.get(e.paid_from_id) if e.paid_from_id else None) or default_cash
            acct = e.account_id or c["other"]
            self._add([(acct, e.amount), (cash, -e.amount)], e.date, "expense", e.id,
                      e.payee or e.memo, f"/expenses?year={e.date.year}&edit={e.id}")

        # Payroll
        run_extra: dict[int, tuple[int, int]] = {}  # pay run -> (bank gl, amount debited beyond net pay)
        for t in txns:
            if t.pay_run_id and t.account_id in bank_gl:
                run_extra[t.pay_run_id] = (bank_gl[t.account_id], -t.amount)
        for run in db.scalars(select(PayRun).where(PayRun.status == "final")):
            url = f"/payroll/{run.id}"
            for pc in run.paychecks:
                cash = via.get(("paycheck", pc.id)) or via.get(("payrun", run.id)) or default_cash
                comp = c["officer_comp"] if pc.employee.is_officer else c["wages"]
                self._add([
                    (comp, pc.gross), (c["payroll_tax"], pc.employer_taxes),
                    (cash, -pc.net), (c["fed_payroll_liab"], -pc.liability_941),
                    (c["futa_liab"], -pc.futa), (c["suta_liab"], -pc.suta),
                ], run.pay_date, "payroll", run.id, f"Payroll {pc.employee.name}", url)
            if run.id in run_extra:
                # A payroll service debited net pay + taxes in one amount: the taxes part pays the liabilities.
                gl, debited = run_extra[run.id]
                extra = debited - sum(pc.net for pc in run.paychecks)
                if extra > 0:
                    parts, left = [], extra
                    for code, owed in (("fed_payroll_liab", sum(pc.liability_941 for pc in run.paychecks)),
                                       ("futa_liab", sum(pc.futa for pc in run.paychecks)),
                                       ("suta_liab", sum(pc.suta for pc in run.paychecks))):
                        take = min(left, owed)
                        parts.append((c[code], take))
                        left -= take
                    parts.append((c["bank_fees"], left))
                    self._add(parts + [(gl, -extra)], run.pay_date, "payroll", run.id,
                              "Payroll taxes paid by payroll service", url)

        # Federal tax deposits
        for d in db.scalars(select(TaxDeposit)):
            cash = via.get(("taxdep", d.id), default_cash)
            liab = c["fed_payroll_liab"] if d.form == "941" else c["futa_liab"]
            self._add([(liab, d.amount), (cash, -d.amount)], d.date, "tax_deposit", d.id,
                      f"Form {d.form} deposit {d.year}{' Q' + str(d.quarter) if d.quarter else ''}", f"/taxes?year={d.year}")

        # Bank transactions categorized straight to an account
        for t in txns:
            if t.gl_account_id and t.status == "matched" and t.account_id in bank_gl:
                self._add([(bank_gl[t.account_id], t.amount), (t.gl_account_id, -t.amount)], t.date, "bank", t.id,
                          t.description, f"/bank/{t.account_id}?show=all")

        # Manual journal entries
        for je in db.scalars(select(JournalEntry)):
            self._add([(ln.account_id, ln.debit - ln.credit) for ln in je.lines], je.date, "journal", je.id,
                      je.memo, f"/books/journal/{je.id}")

        self.postings.sort(key=lambda p: (p.date, p.source, p.source_id))

    # ---------------------------------------------------------- queries
    def balances(self, start: dt.date | None = None, end: dt.date | None = None) -> dict[int, int]:
        out: dict[int, int] = defaultdict(int)
        for p in self.postings:
            if (start and p.date < start) or (end and p.date > end):
                continue
            out[p.account_id] += p.amount
        return out

    def natural(self, account_id: int, amount: int) -> int:
        """Show a debit/credit sum the way people read the account (credit-normal accounts flipped)."""
        return amount if self.accounts[account_id].debit_normal else -amount

    def ledger(self, account_id: int, start: dt.date | None, end: dt.date | None):
        opening = sum(p.amount for p in self.postings if p.account_id == account_id and start and p.date < start)
        rows, running = [], opening
        for p in self.postings:
            if p.account_id != account_id or (start and p.date < start) or (end and p.date > end):
                continue
            running += p.amount
            rows.append((p, running))
        return opening, rows


# ---------------------------------------------------------------- reports
@dataclass
class Section:
    title: str
    rows: list[tuple[Account, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(a for _, a in self.rows)


def _sorted(accts):
    return sorted(accts, key=lambda a: a.number)


def profit_and_loss(books: Books, start: dt.date, end: dt.date) -> dict:
    bal = books.balances(start, end)
    income, cogs, expenses, other = Section("Income"), Section("Cost of goods sold"), Section("Expenses"), Section("Other income")
    for a in _sorted(books.accounts.values()):
        amt = bal.get(a.id, 0)
        if not amt:
            continue
        if a.type == "income":
            (other if a.tax_line in ("K4", "5") else income).rows.append((a, -amt))
        elif a.type == "expense":
            (cogs if a.tax_line == "2" else expenses).rows.append((a, amt))
    gross_profit = income.total - cogs.total
    net = gross_profit - expenses.total + other.total
    return dict(income=income, cogs=cogs, expenses=expenses, other=other, gross_profit=gross_profit, net=net,
                start=start, end=end)


def balance_sheet(books: Books, as_of: dt.date, fiscal_start: dt.date | None = None) -> dict:
    """Balance sheet. Income/expense before the fiscal year roll into retained earnings."""
    fiscal_start = fiscal_start or dt.date(as_of.year, 1, 1)
    bal = books.balances(None, as_of)
    prior = books.balances(None, fiscal_start - dt.timedelta(days=1))
    assets, liabilities, equity = Section("Assets"), Section("Liabilities"), Section("Equity")
    retained_id = books.code["retained"]
    dist_id = books.code["distribution"]
    closed_prior = 0  # credit-positive amount closed into retained earnings from earlier years
    for a in books.accounts.values():
        if a.type in ("income", "expense") or a.id == dist_id:
            closed_prior += -prior.get(a.id, 0)
    for a in _sorted(books.accounts.values()):
        amt = bal.get(a.id, 0)
        if a.type == "asset" and amt:
            assets.rows.append((a, amt))
        elif a.type == "liability" and amt:
            liabilities.rows.append((a, -amt))
        elif a.type == "equity":
            shown = -amt
            if a.id == retained_id:
                shown += closed_prior
            elif a.id == dist_id:
                shown = -(amt - prior.get(a.id, 0))  # this year's distributions only
            if shown:
                equity.rows.append((a, shown))
    pl = profit_and_loss(books, fiscal_start, as_of)
    total_equity = equity.total + pl["net"]
    return dict(assets=assets, liabilities=liabilities, equity=equity, net_income=pl["net"],
                total_equity=total_equity, total_le=liabilities.total + total_equity, as_of=as_of,
                balanced=assets.total == liabilities.total + total_equity)


def trial_balance(books: Books, as_of: dt.date) -> list[tuple[Account, int, int]]:
    bal = books.balances(None, as_of)
    rows = []
    for a in _sorted(books.accounts.values()):
        amt = bal.get(a.id, 0)
        if amt:
            rows.append((a, max(amt, 0), max(-amt, 0)))
    return rows
