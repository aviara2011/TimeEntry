"""Bank transaction import, matching to bookkeeping records, and reconciliation."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .bank_import import ParsedTxn
from .models import (
    EXPENSE_CATEGORIES, BankAccount, BankTransaction, Employee, Expense, Invoice, Payment, Paycheck, PayRun,
    Reconciliation, TaxDeposit,
)
from .money import fmt_money

MATCH_WINDOW_DAYS = 10


# ------------------------------------------------------------------ import
@dataclass
class ImportResult:
    added: int = 0
    duplicates: int = 0
    updated: int = 0


def import_transactions(db: Session, account: BankAccount, txns: list[ParsedTxn], source: str) -> ImportResult:
    res = ImportResult()
    existing = {t.external_id: t for t in db.scalars(
        select(BankTransaction).where(BankTransaction.account_id == account.id))}
    for p in txns:
        old = existing.get(p.external_id)
        if old:
            # A pending transaction that has since posted: refresh it.
            if old.pending and not p.pending:
                old.pending, old.date, old.amount, old.description = False, p.date, p.amount, p.description
                res.updated += 1
            else:
                res.duplicates += 1
            continue
        t = BankTransaction(account_id=account.id, date=p.date, amount=p.amount, description=p.description,
                            memo=p.memo, external_id=p.external_id, source=source, pending=p.pending, status="new")
        db.add(t)
        existing[p.external_id] = t
        res.added += 1
    db.flush()
    for t in db.scalars(select(BankTransaction).where(BankTransaction.account_id == account.id,
                                                      BankTransaction.status == "new")):
        auto_match(db, t)
    return res


# ------------------------------------------------------------------ matching
@dataclass
class Suggestion:
    key: str      # action value, e.g. "link:payment:5" or "inv:3"
    label: str
    score: int    # higher is better


def _near(d1: dt.date, d2: dt.date, days: int = MATCH_WINDOW_DAYS) -> bool:
    return abs((d1 - d2).days) <= days


def _linked_ids(db: Session, column) -> set[int]:
    return {i for i in db.scalars(select(column).where(column.is_not(None)))}


def suggestions(db: Session, t: BankTransaction) -> list[Suggestion]:
    out: list[Suggestion] = []
    lo, hi = t.date - dt.timedelta(days=MATCH_WINDOW_DAYS + 30), t.date + dt.timedelta(days=MATCH_WINDOW_DAYS)
    if t.amount > 0:
        used = _linked_ids(db, BankTransaction.payment_id)
        for p in db.scalars(select(Payment).where(Payment.amount == t.amount, Payment.date >= lo, Payment.date <= hi)):
            if p.id not in used and _near(p.date, t.date):
                out.append(Suggestion(f"link:payment:{p.id}", f"Payment already recorded on {p.invoice.number} ({p.date:%m/%d})", 100))
        for inv in db.scalars(select(Invoice).where(Invoice.status.in_(["sent", "draft"]))):
            if inv.balance == t.amount:
                score = 90 if inv.client.name.lower()[:6] in t.description.lower() else 80
                out.append(Suggestion(f"inv:{inv.id}", f"Payment for {inv.number} — {inv.client.name}", score))
            elif 0 < t.amount < inv.balance and inv.client.name.lower()[:6] in t.description.lower():
                out.append(Suggestion(f"inv:{inv.id}", f"Partial payment for {inv.number} — {inv.client.name} (balance {fmt_money(inv.balance)})", 40))
    else:
        amt = -t.amount
        used_exp = _linked_ids(db, BankTransaction.expense_id)
        for e in db.scalars(select(Expense).where(Expense.amount == amt, Expense.date >= lo, Expense.date <= hi)):
            if e.id not in used_exp and _near(e.date, t.date):
                out.append(Suggestion(f"link:expense:{e.id}", f"Expense already entered: {e.payee or EXPENSE_CATEGORIES[e.category][0]} ({e.date:%m/%d})", 100))
        used_dep = _linked_ids(db, BankTransaction.tax_deposit_id)
        for d in db.scalars(select(TaxDeposit).where(TaxDeposit.amount == amt)):
            if d.id not in used_dep and _near(d.date, t.date):
                out.append(Suggestion(f"link:taxdep:{d.id}", f"Tax deposit already recorded: {d.form} {d.year}{' Q' + str(d.quarter) if d.quarter else ''}", 100))
        used_pc = _linked_ids(db, BankTransaction.paycheck_id)
        used_run = _linked_ids(db, BankTransaction.pay_run_id)
        runs = db.scalars(select(PayRun).where(PayRun.status == "final", PayRun.pay_date >= lo, PayRun.pay_date <= hi)).all()
        for run in runs:
            if not _near(run.pay_date, t.date, 5):
                continue
            for pc in run.paychecks:
                if pc.net == amt and pc.id not in used_pc:
                    out.append(Suggestion(f"link:paycheck:{pc.id}", f"Net pay: {pc.employee.name}, {run.pay_date:%m/%d}", 95))
            if run.id not in used_run:
                net = sum(pc.net for pc in run.paychecks)
                with_941 = net + sum(pc.liability_941 for pc in run.paychecks)
                all_taxes = with_941 + sum(pc.futa + pc.suta for pc in run.paychecks)
                if net == amt and len(run.paychecks) > 1:
                    out.append(Suggestion(f"link:payrun:{run.id}", f"Net pay for pay run {run.pay_date:%m/%d}", 90))
                if amt in (with_941, all_taxes):
                    out.append(Suggestion(f"link:payrun:{run.id}", f"Payroll service debit (net pay + taxes) for {run.pay_date:%m/%d}", 85))
        # Payroll taxes paid through EFTPS show up as e.g. "IRS USATAXPYMT". A monthly depositor
        # pays last month's taxes by the 15th, so the deposit usually belongs to last month's quarter.
        if re.search(r"\birs\b|usataxpymt|eftps", t.description, re.I):
            last_month = t.date.replace(day=1) - dt.timedelta(days=1)
            options = [(last_month.year, (last_month.month - 1) // 3 + 1), (t.date.year, (t.date.month - 1) // 3 + 1)]
            for i, (yy, q) in enumerate(dict.fromkeys(options)):
                out.append(Suggestion(f"tax941:{yy}:{q}", f"New Form 941 deposit for {yy} Q{q}", 60 - i * 10))
        cat = remembered_category(db, t)
        if cat:
            out.append(Suggestion(f"exp:{cat}", f"Expense: {EXPENSE_CATEGORIES[cat][0]} (same as last time)", 70))
    return sorted(out, key=lambda s: -s.score)


def normalize_payee(text: str) -> str:
    s = re.sub(r"[^a-z ]", " ", text.lower())
    words = [w for w in s.split() if len(w) > 2 and w not in {"pos", "debit", "card", "purchase", "ach", "the", "www", "com"}]
    return " ".join(words[:3])


def remembered_category(db: Session, t: BankTransaction) -> str | None:
    key = normalize_payee(t.description)
    if not key:
        return None
    q = (select(Expense.category).join(BankTransaction, BankTransaction.expense_id == Expense.id)
         .where(BankTransaction.id != t.id).order_by(Expense.date.desc()))
    for cat, desc in db.execute(q.add_columns(BankTransaction.description)):
        if normalize_payee(desc) == key and cat in EXPENSE_CATEGORIES and cat != "distribution":
            return cat
    return None


def auto_match(db: Session, t: BankTransaction) -> bool:
    """Link automatically only when exactly one existing record matches with certainty."""
    sure = [s for s in suggestions(db, t) if s.score >= 100]
    if len(sure) == 1:
        apply_action(db, t, sure[0].key)
        return True
    return False


# ------------------------------------------------------------------ actions
def action_choices(db: Session, t: BankTransaction) -> list[tuple[str, list[tuple[str, str]]]]:
    """Grouped options for the 'categorize as' dropdown."""
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    if t.amount > 0:
        invs = db.scalars(select(Invoice).where(Invoice.status.in_(["sent", "draft"])).order_by(Invoice.issue_date)).all()
        groups.append(("Customer payment", [(f"inv:{i.id}", f"{i.number} — {i.client.name} ({fmt_money(i.balance)})") for i in invs]))
        groups.append(("Not revenue", [("excl:transfer", "Transfer between my accounts"),
                                       ("excl:contribution", "Owner contribution / loan to company"),
                                       ("excl:refund", "Refund (reduces an expense)"),
                                       ("excl:interest", "Interest income (note for CPA)"),
                                       ("excl:other", "Other / ignore")]))
    else:
        groups.append(("Expense", [(f"exp:{k}", f"{label} (line {line})") for k, (label, line) in EXPENSE_CATEGORIES.items()
                                   if not line.startswith("K") and k != "distribution"]))
        holders = db.scalars(select(Employee).where(Employee.ownership_pct != "0")).all()
        groups.append(("Owners", [(f"dist:{h.id}", f"Distribution to {h.name}") for h in holders]
                       + [("exp:nondeductible", "Non-deductible / personal")]))
        y = t.date.year
        groups.append(("Federal tax payment", [(f"tax941:{yy}:{q}", f"Form 941 deposit {yy} Q{q}") for yy in (y - 1, y) for q in (1, 2, 3, 4)]
                       + [(f"tax940:{yy}", f"Form 940 (FUTA) deposit {yy}") for yy in (y - 1, y)]))
        groups.append(("Not an expense", [("excl:transfer", "Transfer between my accounts"),
                                          ("excl:payroll", "Payroll (handled in Payroll)"),
                                          ("excl:cc_payment", "Credit card payment"),
                                          ("excl:loan", "Loan principal repayment"),
                                          ("excl:other", "Other / ignore")]))
    return groups


EXCLUDE_LABELS = {
    "transfer": "Transfer", "contribution": "Owner contribution / loan", "refund": "Refund", "interest": "Interest income",
    "payroll": "Payroll", "cc_payment": "Credit card payment", "loan": "Loan repayment", "other": "Ignored",
}


def apply_action(db: Session, t: BankTransaction, key: str) -> str:
    """Categorize/match a bank transaction. Returns a short description."""
    if t.status != "new":
        unmatch(db, t)
    kind, _, rest = key.partition(":")
    amt = abs(t.amount)
    if kind == "link":
        what, _, ident = rest.partition(":")
        col = {"payment": "payment_id", "expense": "expense_id", "taxdep": "tax_deposit_id",
               "paycheck": "paycheck_id", "payrun": "pay_run_id"}[what]
        setattr(t, col, int(ident))
        t.created_record = False
        msg = "Matched to existing record"
    elif kind == "inv":
        inv = db.get(Invoice, int(rest))
        if inv.status == "draft":
            inv.status = "sent"
        pay = Payment(invoice_id=inv.id, date=t.date, amount=amt, method="Bank deposit", memo=t.description[:200])
        inv.payments.append(pay)
        db.flush()
        if inv.total > 0 and inv.paid >= inv.total:
            inv.status = "paid"
        t.payment_id, t.created_record = pay.id, True
        msg = f"Recorded payment on {inv.number}"
    elif kind in ("exp", "dist"):
        cat = rest if kind == "exp" else "distribution"
        if cat not in EXPENSE_CATEGORIES:
            raise ValueError("Unknown category")
        exp = Expense(date=t.date, payee=t.description[:200], category=cat, amount=amt, memo=t.memo,
                      shareholder_id=int(rest) if kind == "dist" else None)
        db.add(exp)
        db.flush()
        t.expense_id, t.created_record = exp.id, True
        msg = f"Recorded as {EXPENSE_CATEGORIES[cat][0]}"
    elif kind in ("tax941", "tax940"):
        parts = rest.split(":")
        dep = TaxDeposit(date=t.date, form=kind[3:], year=int(parts[0]),
                         quarter=int(parts[1]) if kind == "tax941" else None, amount=amt, confirmation="bank")
        db.add(dep)
        db.flush()
        t.tax_deposit_id, t.created_record = dep.id, True
        msg = f"Recorded Form {dep.form} deposit"
    elif kind == "excl":
        t.status, t.note = "excluded", EXCLUDE_LABELS.get(rest, rest)
        return t.note
    else:
        raise ValueError(f"Unknown action {key!r}")
    t.status = "matched"
    return msg


def unmatch(db: Session, t: BankTransaction) -> None:
    """Undo a categorization. Records this screen created are deleted; pre-existing ones are just unlinked."""
    if t.created_record:
        if t.payment_id:
            pay = db.get(Payment, t.payment_id)
            if pay:
                inv = pay.invoice
                inv.payments.remove(pay)
                db.flush()
                if inv.status == "paid":
                    inv.status = "sent"
        for col, model in (("expense_id", Expense), ("tax_deposit_id", TaxDeposit)):
            rid = getattr(t, col)
            if rid and (rec := db.get(model, rid)):
                db.delete(rec)
    t.payment_id = t.expense_id = t.tax_deposit_id = t.paycheck_id = t.pay_run_id = None
    t.created_record = False
    t.status, t.note = "new", ""
    db.flush()


def describe_link(t: BankTransaction) -> str:
    if t.status == "excluded":
        return t.note
    if t.payment:
        return f"Payment · {t.payment.invoice.number} {t.payment.invoice.client.name}"
    if t.expense:
        label = EXPENSE_CATEGORIES.get(t.expense.category, ("Expense",))[0]
        return f"{label}{' · ' + t.expense.shareholder.name if t.expense.shareholder else ''}"
    if t.tax_deposit:
        d = t.tax_deposit
        return f"Form {d.form} deposit {d.year}{' Q' + str(d.quarter) if d.quarter else ''}"
    if t.paycheck:
        return f"Net pay · {t.paycheck.employee.name}"
    if t.pay_run:
        return f"Payroll {t.pay_run.pay_date:%m/%d/%Y}"
    return ""


# ------------------------------------------------------------------ balances & reconciliation
def book_balance(db: Session, account: BankAccount, through: dt.date | None = None) -> int:
    q = select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
        BankTransaction.account_id == account.id, BankTransaction.pending.is_(False))
    if through:
        q = q.where(BankTransaction.date <= through)
    return account.opening_balance + db.scalar(q)


def reconciled_balance(db: Session, account: BankAccount) -> int:
    done = select(Reconciliation.id).where(Reconciliation.account_id == account.id, Reconciliation.status == "done")
    total = db.scalar(select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
        BankTransaction.account_id == account.id, BankTransaction.reconciliation_id.in_(done)))
    return account.opening_balance + total


def last_reconciliation(db: Session, account: BankAccount) -> Reconciliation | None:
    return db.scalar(select(Reconciliation).where(Reconciliation.account_id == account.id, Reconciliation.status == "done")
                     .order_by(Reconciliation.statement_date.desc(), Reconciliation.id.desc()).limit(1))


def open_reconciliation(db: Session, account: BankAccount) -> Reconciliation | None:
    return db.scalar(select(Reconciliation).where(Reconciliation.account_id == account.id, Reconciliation.status == "open"))


def reconcile_candidates(db: Session, rec: Reconciliation) -> list[BankTransaction]:
    """Unreconciled, posted transactions up to the statement date (plus anything already ticked)."""
    return db.scalars(select(BankTransaction).where(
        BankTransaction.account_id == rec.account_id, BankTransaction.pending.is_(False),
        ((BankTransaction.reconciliation_id.is_(None)) & (BankTransaction.date <= rec.statement_date))
        | (BankTransaction.reconciliation_id == rec.id),
    ).order_by(BankTransaction.date, BankTransaction.id)).all()


def cleared_balance(db: Session, rec: Reconciliation) -> int:
    ticked = db.scalar(select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
        BankTransaction.reconciliation_id == rec.id))
    return rec.beginning_balance + ticked
