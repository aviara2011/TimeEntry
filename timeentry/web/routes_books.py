"""Books: chart of accounts, account ledgers, financial statements, journal entries."""
from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ledger import Books, balance_sheet, profit_and_loss, trial_balance
from ..models import ACCOUNT_TYPES, TAX_LINES, Account, BankAccount, JournalEntry, JournalLine
from ..money import parse_money
from .common import checked, get_company, get_db, parse_date, redirect, render

router = APIRouter(prefix="/books")


def _basis(request: Request, db: Session) -> str:
    b = request.query_params.get("basis")
    return b if b in ("cash", "accrual") else get_company(db).accounting_method


def _range(request: Request) -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    qp = request.query_params
    return parse_date(qp.get("start"), dt.date(today.year, 1, 1)), parse_date(qp.get("end"), today)


@router.get("")
def books_home(request: Request, db: Session = Depends(get_db)):
    basis = _basis(request, db)
    books = Books(db, basis)
    today = dt.date.today()
    pl = profit_and_loss(books, dt.date(today.year, 1, 1), today)
    bs = balance_sheet(books, today)
    return render(request, "books.html", pl=pl, bs=bs, basis=basis)


# ------------------------------------------------------------------ chart of accounts
@router.get("/accounts")
def accounts(request: Request, db: Session = Depends(get_db)):
    basis = _basis(request, db)
    books = Books(db, basis)
    bal = books.balances(None, dt.date.today())
    rows = sorted(books.accounts.values(), key=lambda a: a.number)
    show_inactive = request.query_params.get("inactive") == "1"
    return render(request, "accounts.html", rows=[(a, books.natural(a.id, bal.get(a.id, 0))) for a in rows
                                                  if a.active or show_inactive], basis=basis, show_inactive=show_inactive)


@router.get("/accounts/{account_id}")
@router.get("/accounts/new")
def account_view(request: Request, account_id: int | None = None, db: Session = Depends(get_db)):
    if account_id is None:
        acct = Account(number="", name="", type=request.query_params.get("type", "expense"), tax_line="",
                       deductible_pct=100, active=True, is_system=False, description="")
        return render(request, "account.html", acct=acct, rows=[], opening=0, books=None)
    acct = db.get(Account, account_id)
    if not acct:
        return redirect("/books/accounts", err="Account not found")
    basis = _basis(request, db)
    books = Books(db, basis)
    start, end = _range(request)
    if acct.type in ("asset", "liability", "equity") and not request.query_params.get("start"):
        start = dt.date(1900, 1, 1)
    opening, rows = books.ledger(acct.id, start, end)
    bank = db.scalar(select(BankAccount).where(BankAccount.gl_account_id == acct.id))
    return render(request, "account.html", acct=acct, rows=rows, opening=opening, books=books, basis=basis,
                  start=start, end=end, bank=bank)


@router.post("/accounts/save")
async def account_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    acct = db.get(Account, int(f["id"])) if f.get("id") else Account(is_system=False)
    back = f"/books/accounts/{acct.id or 'new'}"
    number = f.get("number", "").strip()
    name = f.get("name", "").strip()
    if not number or not name:
        return redirect(back, err="Number and name are required")
    clash = db.scalar(select(Account).where(Account.number == number, Account.id != (acct.id or 0)))
    if clash:
        return redirect(back, err=f"Number {number} is already used by {clash.name}")
    typ = f.get("type", acct.type)
    if typ not in ACCOUNT_TYPES:
        return redirect(back, err="Choose a type")
    if acct.is_system and acct.type and typ != acct.type:
        return redirect(back, err="The type of a built-in account can't be changed")
    line = f.get("tax_line", "")
    if line not in TAX_LINES:
        return redirect(back, err="Unknown tax line")
    acct.number, acct.name, acct.type, acct.tax_line = number, name, typ, line
    try:
        acct.deductible_pct = max(0, min(100, int(f.get("deductible_pct") or 100)))
    except ValueError:
        return redirect(back, err="Deductible % must be a whole number")
    acct.description = f.get("description", "")
    acct.active = checked(f, "active") if f.get("id") else True
    if acct.is_system and not acct.active:
        return redirect(back, err="Built-in accounts can't be made inactive")
    db.add(acct)
    db.commit()
    return redirect(f"/books/accounts/{acct.id}", msg="Account saved")


@router.post("/accounts/{account_id}/delete")
def account_delete(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(Account, account_id)
    if acct.is_system:
        return redirect(f"/books/accounts/{account_id}", err="Built-in accounts can't be deleted")
    books = Books(db, "accrual")
    used = any(p.account_id == acct.id for p in books.postings) or any(
        p.account_id == acct.id for p in Books(db, "cash").postings)
    if used:
        return redirect(f"/books/accounts/{account_id}", err="This account has transactions. Make it inactive instead.")
    db.delete(acct)
    db.commit()
    return redirect("/books/accounts", msg="Account deleted")


# ------------------------------------------------------------------ statements
@router.get("/pl")
def pl(request: Request, db: Session = Depends(get_db)):
    basis = _basis(request, db)
    start, end = _range(request)
    report = profit_and_loss(Books(db, basis), start, end)
    if request.query_params.get("format") == "csv":
        rows = [("Account", "Amount")]
        for sec in ("income", "cogs", "expenses", "other"):
            rows += [(a.label, f"{v / 100:.2f}") for a, v in report[sec].rows]
        rows.append(("Net income", f"{report['net'] / 100:.2f}"))
        return _csv(rows, f"profit-loss-{start}-{end}.csv")
    return render(request, "pl.html", r=report, basis=basis, start=start, end=end)


@router.get("/balance-sheet")
def bs(request: Request, db: Session = Depends(get_db)):
    basis = _basis(request, db)
    as_of = parse_date(request.query_params.get("as_of"), dt.date.today())
    return render(request, "balance_sheet.html", r=balance_sheet(Books(db, basis), as_of), basis=basis, as_of=as_of)


@router.get("/trial-balance")
def tb(request: Request, db: Session = Depends(get_db)):
    basis = _basis(request, db)
    as_of = parse_date(request.query_params.get("as_of"), dt.date.today())
    rows = trial_balance(Books(db, basis), as_of)
    if request.query_params.get("format") == "csv":
        return _csv([("Number", "Account", "Debit", "Credit")] + [(a.number, a.name, f"{d / 100:.2f}", f"{c / 100:.2f}") for a, d, c in rows],
                    f"trial-balance-{as_of}.csv")
    return render(request, "trial_balance.html", rows=rows, basis=basis, as_of=as_of,
                  debits=sum(d for _, d, _ in rows), credits=sum(c for _, _, c in rows))


@router.get("/general-ledger.csv")
def gl_csv(request: Request, db: Session = Depends(get_db)):
    basis = _basis(request, db)
    start, end = _range(request)
    books = Books(db, basis)
    rows = [("Date", "Account number", "Account", "Debit", "Credit", "Source", "Memo")]
    for p in books.postings:
        if start <= p.date <= end:
            a = books.accounts[p.account_id]
            rows.append((p.date.isoformat(), a.number, a.name, f"{max(p.amount, 0) / 100:.2f}",
                         f"{max(-p.amount, 0) / 100:.2f}", p.source, p.memo))
    return _csv(rows, f"general-ledger-{start}-{end}.csv")


def _csv(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


# ------------------------------------------------------------------ journal entries
@router.get("/journal")
def journal(request: Request, db: Session = Depends(get_db)):
    entries = db.scalars(select(JournalEntry).order_by(JournalEntry.date.desc(), JournalEntry.id.desc())).all()
    return render(request, "journal.html", entries=entries)


@router.get("/journal/{entry_id}")
@router.get("/journal/new")
def journal_form(request: Request, entry_id: int | None = None, db: Session = Depends(get_db)):
    je = db.get(JournalEntry, entry_id) if entry_id else None
    accounts = db.scalars(select(Account).where(Account.active.is_(True)).order_by(Account.number)).all()
    lines = list(je.lines) if je else []
    blanks = max(2, 6 - len(lines))
    return render(request, "journal_form.html", je=je, lines=lines, blanks=blanks, accounts=accounts)


@router.post("/journal/save")
async def journal_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    je = db.get(JournalEntry, int(f["id"])) if f.get("id") else JournalEntry()
    back = f"/books/journal/{je.id or 'new'}"
    try:
        je.date = parse_date(f.get("date"))
        je.memo = f.get("memo", "").strip()
        new_lines = []
        for acct_id, debit, credit, memo in zip(f.getlist("account_id"), f.getlist("debit"), f.getlist("credit"),
                                                f.getlist("line_memo")):
            d, c = parse_money(debit), parse_money(credit)
            if not acct_id and not d and not c:
                continue
            if not acct_id:
                raise ValueError("Each line with an amount needs an account")
            if d and c:
                raise ValueError("A line can have a debit or a credit, not both")
            if d < 0 or c < 0:
                raise ValueError("Use positive amounts")
            new_lines.append(JournalLine(account_id=int(acct_id), debit=d, credit=c, memo=memo))
        if len(new_lines) < 2:
            raise ValueError("A journal entry needs at least two lines")
        diff = sum(ln.debit for ln in new_lines) - sum(ln.credit for ln in new_lines)
        if diff:
            raise ValueError(f"Debits and credits differ by ${abs(diff) / 100:,.2f}")
    except ValueError as exc:
        return redirect(back, err=str(exc))
    je.lines.clear()
    je.lines.extend(new_lines)
    db.add(je)
    db.commit()
    return redirect("/books/journal", msg="Journal entry saved")


@router.post("/journal/{entry_id}/delete")
def journal_delete(entry_id: int, db: Session = Depends(get_db)):
    je = db.get(JournalEntry, entry_id)
    if je:
        db.delete(je)
        db.commit()
    return redirect("/books/journal", msg="Journal entry deleted")
