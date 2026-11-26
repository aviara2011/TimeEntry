"""Bank accounts: import / sync transactions, review & categorize, reconcile."""
from __future__ import annotations

import datetime as dt

import httpx
from fastapi import APIRouter, Depends, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import simplefin
from ..bank_import import parse_file
from ..banking import (
    action_choices, apply_action, book_balance, cleared_balance, describe_link, import_transactions,
    last_reconciliation, open_reconciliation, reconcile_candidates, reconciled_balance, suggestions, unmatch,
)
from ..models import BankAccount, BankConnection, BankTransaction, Reconciliation
from ..money import fmt_money, parse_money
from .common import checked, get_db, parse_date, redirect, render

router = APIRouter(prefix="/bank")


def _connection(db: Session) -> BankConnection | None:
    return db.scalar(select(BankConnection).limit(1))


@router.get("")
def bank_home(request: Request, db: Session = Depends(get_db)):
    accounts = db.scalars(select(BankAccount).order_by(BankAccount.active.desc(), BankAccount.name)).all()
    rows = []
    for a in accounts:
        rows.append(dict(
            a=a, book=book_balance(db, a),
            new=db.scalar(select(func.count(BankTransaction.id)).where(
                BankTransaction.account_id == a.id, BankTransaction.status == "new", BankTransaction.pending.is_(False))),
            last_rec=last_reconciliation(db, a),
        ))
    return render(request, "bank.html", rows=rows, conn=_connection(db))


@router.post("/accounts/save")
async def account_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    a = db.get(BankAccount, int(f["id"])) if f.get("id") else BankAccount()
    try:
        a.name = f.get("name", "").strip()
        if not a.name:
            raise ValueError("Account name is required")
        a.institution = f.get("institution", "")
        a.last4 = f.get("last4", "")[-4:]
        a.kind = f.get("kind", "checking")
        a.opening_balance = parse_money(f.get("opening_balance"))
        a.opening_date = parse_date(f["opening_date"]) if f.get("opening_date") else None
        a.active = checked(f, "active") if f.get("id") else True
    except ValueError as exc:
        return redirect("/bank", err=str(exc))
    db.add(a)
    db.commit()
    return redirect(f"/bank/{a.id}", msg="Account saved")


@router.get("/{account_id}")
def register(account_id: int, request: Request, db: Session = Depends(get_db)):
    a = db.get(BankAccount, account_id)
    if not a:
        return redirect("/bank", err="Account not found")
    show = request.query_params.get("show", "new")
    q = select(BankTransaction).where(BankTransaction.account_id == a.id)
    if show in ("new", "matched", "excluded"):
        q = q.where(BankTransaction.status == show)
    txns = db.scalars(q.order_by(BankTransaction.date.desc(), BankTransaction.id.desc()).limit(500)).all()
    sugg = {t.id: suggestions(db, t)[:3] for t in txns if t.status == "new" and not t.pending}
    choices = {t.id: action_choices(db, t) for t in txns if t.status == "new" and not t.pending}
    counts = dict(db.execute(select(BankTransaction.status, func.count()).where(
        BankTransaction.account_id == a.id).group_by(BankTransaction.status)).all())
    return render(request, "bank_register.html", a=a, txns=txns, show=show, sugg=sugg, choices=choices,
                  counts=counts, book=book_balance(db, a), describe=describe_link,
                  rec_open=open_reconciliation(db, a), last_rec=last_reconciliation(db, a))


@router.post("/{account_id}/import")
async def import_file(account_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    a = db.get(BankAccount, account_id)
    upload: UploadFile = f.get("file")
    if not upload or not upload.filename:
        return redirect(f"/bank/{account_id}", err="Choose a file to import")
    data = await upload.read()
    try:
        st = parse_file(upload.filename, data, flip_sign=checked(f, "flip_sign"))
    except ValueError as exc:
        return redirect(f"/bank/{account_id}", err=f"Couldn't read {upload.filename}: {exc}")
    res = import_transactions(db, a, st.transactions, "file")
    if st.balance is not None:
        a.bank_balance, a.bank_balance_date = st.balance, st.balance_date
    if st.account_number and not a.last4:
        a.last4 = st.account_number[-4:]
    db.commit()
    return redirect(f"/bank/{account_id}", msg=f"Imported {res.added} new transactions ({res.duplicates} already here)")


@router.post("/txn/{txn_id}/action")
async def txn_action(txn_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    t = db.get(BankTransaction, txn_id)
    back = f.get("back") or f"/bank/{t.account_id}"
    if t.reconciliation_id and db.get(Reconciliation, t.reconciliation_id).status == "done":
        return redirect(back, err="This transaction is in a completed reconciliation")
    key = f.get("action", "")
    if not key:
        return redirect(back, err="Choose what this transaction is")
    try:
        msg = apply_action(db, t, key)
    except (ValueError, KeyError) as exc:
        db.rollback()
        return redirect(back, err=str(exc))
    db.commit()
    return redirect(back, msg=f"{t.date:%m/%d} {fmt_money(t.amount)}: {msg}")


@router.post("/txn/{txn_id}/unmatch")
async def txn_unmatch(txn_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    t = db.get(BankTransaction, txn_id)
    unmatch(db, t)
    db.commit()
    return redirect(f.get("back") or f"/bank/{t.account_id}", msg="Moved back to review")


# ------------------------------------------------------------------ SimpleFIN
@router.post("/simplefin/connect")
async def sf_connect(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    try:
        access_url = simplefin.claim_setup_token(f.get("token", ""))
    except (ValueError, httpx.HTTPError) as exc:
        return redirect("/bank", err=f"Couldn't connect: {exc}")
    conn = _connection(db) or BankConnection(access_url="")
    conn.access_url, conn.last_error = access_url, ""
    db.add(conn)
    db.commit()
    return await _sync(db, days=90)


@router.post("/simplefin/sync")
async def sf_sync(db: Session = Depends(get_db)):
    return await _sync(db)


async def _sync(db: Session, days: int | None = None):
    conn = _connection(db)
    if not conn:
        return redirect("/bank", err="Connect SimpleFIN first")
    linked = db.scalars(select(BankAccount).where(BankAccount.simplefin_id.is_not(None))).all()
    last = min((a.last_sync for a in linked if a.last_sync), default=None)
    start = (last.date() - dt.timedelta(days=7)) if (last and days is None) else dt.date.today() - dt.timedelta(days=days or 90)
    try:
        accounts, errors = simplefin.fetch_accounts(conn.access_url, start)
    except (ValueError, httpx.HTTPError) as exc:
        conn.last_error = str(exc)
        db.commit()
        return redirect("/bank", err=f"Sync failed: {exc}")
    added = 0
    for sa in accounts:
        a = db.scalar(select(BankAccount).where(BankAccount.simplefin_id == sa.id))
        new_account = a is None
        if new_account:
            a = BankAccount(name=sa.name or "Bank account", institution=sa.institution, simplefin_id=sa.id,
                            kind="credit_card" if "card" in sa.name.lower() else "checking", opening_balance=0)
            db.add(a)
            db.flush()
        res = import_transactions(db, a, sa.transactions, "simplefin")
        added += res.added
        a.bank_balance, a.bank_balance_date = sa.balance, sa.balance_date or dt.date.today()
        a.last_sync = dt.datetime.now()
        if new_account:
            # Start the books so our balance equals the bank's balance today.
            posted = sum(t.amount for t in sa.transactions if not t.pending)
            a.opening_balance = sa.balance - posted
            a.opening_date = min((t.date for t in sa.transactions), default=dt.date.today())
    conn.last_error = "; ".join(errors)
    db.commit()
    msg = f"Synced {len(accounts)} account(s), {added} new transaction(s)"
    return redirect("/bank", msg=msg + (f". Bank messages: {conn.last_error}" if errors else ""))


@router.post("/simplefin/disconnect")
def sf_disconnect(db: Session = Depends(get_db)):
    conn = _connection(db)
    if conn:
        db.delete(conn)
        db.commit()
    return redirect("/bank", msg="Disconnected. Also revoke this app's access at SimpleFIN Bridge.")


# ------------------------------------------------------------------ reconciliation
@router.get("/{account_id}/reconcile")
def reconcile(account_id: int, request: Request, db: Session = Depends(get_db)):
    a = db.get(BankAccount, account_id)
    rec = open_reconciliation(db, a)
    history = db.scalars(select(Reconciliation).where(Reconciliation.account_id == a.id, Reconciliation.status == "done")
                         .order_by(Reconciliation.statement_date.desc())).all()
    ctx = dict(a=a, rec=rec, history=history, last=last_reconciliation(db, a))
    if rec:
        ctx.update(txns=reconcile_candidates(db, rec), cleared=cleared_balance(db, rec), describe=describe_link)
    else:
        ctx.update(beginning=reconciled_balance(db, a))
    return render(request, "bank_reconcile.html", **ctx)


@router.post("/{account_id}/reconcile/start")
async def reconcile_start(account_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    a = db.get(BankAccount, account_id)
    if open_reconciliation(db, a):
        return redirect(f"/bank/{account_id}/reconcile")
    try:
        rec = Reconciliation(account_id=a.id, statement_date=parse_date(f.get("statement_date")),
                             statement_balance=parse_money(f.get("statement_balance")),
                             beginning_balance=reconciled_balance(db, a), status="open")
    except ValueError as exc:
        return redirect(f"/bank/{account_id}/reconcile", err=str(exc))
    last = last_reconciliation(db, a)
    if last and rec.statement_date <= last.statement_date:
        return redirect(f"/bank/{account_id}/reconcile", err="Statement date must be after the last reconciliation")
    db.add(rec)
    db.flush()
    # Pre-tick everything dated on or before the statement date; untick what isn't on the statement.
    for t in reconcile_candidates(db, rec):
        t.reconciliation_id = rec.id
    db.commit()
    return redirect(f"/bank/{account_id}/reconcile")


@router.post("/reconcile/{rec_id}/save")
async def reconcile_save(rec_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    rec = db.get(Reconciliation, rec_id)
    if rec.status != "open":
        return redirect(f"/bank/{rec.account_id}/reconcile")
    ticked = {int(x) for x in f.getlist("cleared")}
    for t in reconcile_candidates(db, rec):
        t.reconciliation_id = rec.id if t.id in ticked else None
    db.flush()
    if f.get("finish"):
        diff = rec.statement_balance - cleared_balance(db, rec)
        if diff != 0:
            db.commit()
            return redirect(f"/bank/{rec.account_id}/reconcile",
                            err=f"Off by {fmt_money(diff)}. Untick items not on the statement, or import missing ones.")
        rec.status, rec.completed_at = "done", dt.datetime.now()
        db.commit()
        return redirect(f"/bank/{rec.account_id}/reconcile", msg=f"Reconciled through {rec.statement_date:%m/%d/%Y}")
    if f.get("cancel"):
        for t in db.scalars(select(BankTransaction).where(BankTransaction.reconciliation_id == rec.id)):
            t.reconciliation_id = None
        db.delete(rec)
        db.commit()
        return redirect(f"/bank/{rec.account_id}/reconcile", msg="Reconciliation canceled")
    db.commit()
    return redirect(f"/bank/{rec.account_id}/reconcile")


@router.post("/reconcile/{rec_id}/undo")
def reconcile_undo(rec_id: int, db: Session = Depends(get_db)):
    rec = db.get(Reconciliation, rec_id)
    a = rec.account
    if last_reconciliation(db, a).id != rec.id or open_reconciliation(db, a):
        return redirect(f"/bank/{a.id}/reconcile", err="Only the most recent reconciliation can be undone")
    rec.status = "open"
    db.commit()
    return redirect(f"/bank/{a.id}/reconcile", msg="Reopened the last reconciliation")
