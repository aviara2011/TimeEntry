"""Invoices, invoice lines and customer payments."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, Invoice, InvoiceLine, Payment, Project, TimeEntry
from ..money import amount_for_minutes, fmt_hours, fmt_money, parse_money, round_cents
from .common import get_company, get_db, parse_date, redirect, render

router = APIRouter(prefix="/invoices")


def unbilled_entries(db: Session, client_id: int, start: dt.date | None, end: dt.date | None):
    q = (
        select(TimeEntry).join(Project)
        .where(Project.client_id == client_id, TimeEntry.billable.is_(True), TimeEntry.invoice_id.is_(None),
               TimeEntry.timer_started_at.is_(None), TimeEntry.minutes > 0)
    )
    if start:
        q = q.where(TimeEntry.date >= start)
    if end:
        q = q.where(TimeEntry.date <= end)
    return db.scalars(q.order_by(TimeEntry.date, TimeEntry.id)).all()


def _sync_status(inv: Invoice) -> None:
    if inv.status in ("sent", "paid"):
        inv.status = "paid" if inv.total > 0 and inv.paid >= inv.total else "sent"


@router.get("")
def invoice_list(request: Request, db: Session = Depends(get_db)):
    status = request.query_params.get("status") or ""
    q = select(Invoice).order_by(Invoice.issue_date.desc(), Invoice.id.desc())
    if status:
        q = q.where(Invoice.status == status)
    invoices = db.scalars(q).all()
    return render(request, "invoices.html", invoices=invoices, status=status,
                  clients=db.scalars(select(Client).where(Client.active.is_(True)).order_by(Client.name)).all())


@router.get("/new")
def invoice_new(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    client_id = int(qp["client_id"]) if qp.get("client_id") else None
    start = parse_date(qp["start"]) if qp.get("start") else None
    end = parse_date(qp["end"]) if qp.get("end") else None
    entries = unbilled_entries(db, client_id, start, end) if client_id else []
    return render(
        request, "invoice_new.html", client=db.get(Client, client_id) if client_id else None,
        clients=db.scalars(select(Client).where(Client.active.is_(True)).order_by(Client.name)).all(),
        entries=entries, start=start, end=end, amount_for_minutes=amount_for_minutes,
    )


@router.post("/create")
async def invoice_create(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    company = get_company(db)
    client = db.get(Client, int(f["client_id"]))
    issue = parse_date(f.get("issue_date"), dt.date.today())
    terms = client.terms_days if client.terms_days is not None else company.payment_terms_days
    inv = Invoice(
        number=f"{company.invoice_prefix}{company.next_invoice_number}", client_id=client.id,
        issue_date=issue, due_date=issue + dt.timedelta(days=terms), status="draft", notes="",
    )
    company.next_invoice_number += 1
    db.add(inv)
    db.flush()
    ids = [int(x) for x in f.getlist("entry_ids")]
    entries = db.scalars(select(TimeEntry).where(TimeEntry.id.in_(ids)).order_by(TimeEntry.date, TimeEntry.id)).all() if ids else []
    for pos, e in enumerate(entries):
        if e.invoice_id:
            continue
        rate = e.project.effective_rate
        desc = f"{e.project.name}: {e.description}" if e.description else e.project.name
        if e.employee:
            desc += f" ({e.employee.first_name})"
        db.add(InvoiceLine(
            invoice_id=inv.id, position=pos, time_entry_id=e.id, date=e.date, description=desc,
            quantity=fmt_hours(e.minutes), unit_price=rate, amount=amount_for_minutes(e.minutes, rate),
        ))
        e.invoice_id = inv.id
    db.commit()
    return redirect(f"/invoices/{inv.id}", msg=f"Invoice {inv.number} created with {len(entries)} time entries")


@router.get("/{invoice_id}")
def invoice_view(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        return redirect("/invoices", err="Invoice not found")
    return render(request, "invoice.html", inv=inv, company=get_company(db))


@router.get("/{invoice_id}/print")
def invoice_print(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    return render(request, "invoice_print.html", inv=inv, company=get_company(db))


@router.post("/{invoice_id}/update")
async def invoice_update(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    inv = db.get(Invoice, invoice_id)
    try:
        inv.issue_date = parse_date(f.get("issue_date"), inv.issue_date)
        inv.due_date = parse_date(f.get("due_date"), inv.due_date)
        inv.notes = f.get("notes", "")
    except ValueError as exc:
        return redirect(f"/invoices/{invoice_id}", err=str(exc))
    db.commit()
    return redirect(f"/invoices/{invoice_id}", msg="Invoice updated")


@router.post("/{invoice_id}/lines")
async def line_add(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    inv = db.get(Invoice, invoice_id)
    if inv.status != "draft":
        return redirect(f"/invoices/{invoice_id}", err="Only draft invoices can be changed")
    try:
        qty = Decimal(f.get("quantity") or "1")
        price = parse_money(f.get("unit_price"))
    except (InvalidOperation, ValueError):
        return redirect(f"/invoices/{invoice_id}", err="Enter a valid quantity and price")
    desc = f.get("description", "").strip()
    if not desc:
        return redirect(f"/invoices/{invoice_id}", err="Description is required")
    db.add(InvoiceLine(
        invoice_id=inv.id, position=len(inv.lines), description=desc,
        date=parse_date(f["date"]) if f.get("date") else None,
        quantity=str(qty), unit_price=price, amount=round_cents(qty * price),
    ))
    db.commit()
    return redirect(f"/invoices/{invoice_id}", msg="Line added")


@router.post("/{invoice_id}/lines/{line_id}/delete")
def line_delete(invoice_id: int, line_id: int, db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    line = db.get(InvoiceLine, line_id)
    if inv.status != "draft" or not line or line.invoice_id != inv.id:
        return redirect(f"/invoices/{invoice_id}", err="Only lines on draft invoices can be removed")
    if line.time_entry_id:
        entry = db.get(TimeEntry, line.time_entry_id)
        if entry:
            entry.invoice_id = None
    db.delete(line)
    db.commit()
    return redirect(f"/invoices/{invoice_id}", msg="Line removed; its time entry is unbilled again")


@router.post("/{invoice_id}/status")
async def invoice_status(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    inv = db.get(Invoice, invoice_id)
    action = f.get("action")
    if action == "send" and inv.status == "draft":
        if not inv.lines:
            return redirect(f"/invoices/{invoice_id}", err="Add at least one line first")
        inv.status = "sent"
        _sync_status(inv)
    elif action == "reopen" and inv.status == "sent" and not inv.payments:
        inv.status = "draft"
    elif action == "void" and inv.status in ("draft", "sent"):
        if inv.payments:
            return redirect(f"/invoices/{invoice_id}", err="Delete the payments before voiding")
        inv.status = "void"
        for e in db.scalars(select(TimeEntry).where(TimeEntry.invoice_id == inv.id)):
            e.invoice_id = None
        for line in inv.lines:
            line.time_entry_id = None
    elif action == "delete" and inv.status == "draft":
        for e in db.scalars(select(TimeEntry).where(TimeEntry.invoice_id == inv.id)):
            e.invoice_id = None
        db.delete(inv)
        db.commit()
        return redirect("/invoices", msg="Draft deleted; its time entries are unbilled again")
    else:
        return redirect(f"/invoices/{invoice_id}", err="That action isn't allowed for this invoice")
    db.commit()
    return redirect(f"/invoices/{invoice_id}", msg=f"Invoice is now {inv.status}")


@router.post("/{invoice_id}/payments")
async def payment_add(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    inv = db.get(Invoice, invoice_id)
    if inv.status not in ("sent", "paid"):
        return redirect(f"/invoices/{invoice_id}", err="Mark the invoice as sent before recording payments")
    try:
        amount = parse_money(f.get("amount")) if f.get("amount") else inv.balance
        if amount <= 0:
            raise ValueError("Payment amount must be greater than zero")
        date = parse_date(f.get("date"), dt.date.today())
    except ValueError as exc:
        return redirect(f"/invoices/{invoice_id}", err=str(exc))
    inv.payments.append(Payment(date=date, amount=amount, method=f.get("method", ""), memo=f.get("memo", "")))
    db.flush()
    _sync_status(inv)
    db.commit()
    return redirect(f"/invoices/{invoice_id}", msg=f"Recorded payment of {fmt_money(amount)}")


@router.post("/{invoice_id}/payments/{payment_id}/delete")
def payment_delete(invoice_id: int, payment_id: int, db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    pay = db.get(Payment, payment_id)
    if pay and pay.invoice_id == inv.id:
        inv.payments.remove(pay)
        db.flush()
        _sync_status(inv)
        db.commit()
    return redirect(f"/invoices/{invoice_id}", msg="Payment deleted")
