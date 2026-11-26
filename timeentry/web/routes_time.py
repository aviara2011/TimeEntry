"""Time entries and the running timer."""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, Employee, Project, TimeEntry
from ..money import amount_for_minutes, fmt_hours, parse_hours
from .common import checked, get_db, opt_int, parse_date, redirect, render

router = APIRouter(prefix="/time")


def _active_projects(db: Session):
    return db.scalars(
        select(Project).join(Client).where(Project.active.is_(True), Client.active.is_(True)).order_by(Client.name, Project.name)
    ).all()


def _filtered(db: Session, start: dt.date, end: dt.date, client_id: int | None, employee_id: int | None):
    q = select(TimeEntry).join(Project).where(TimeEntry.date >= start, TimeEntry.date <= end)
    if client_id:
        q = q.where(Project.client_id == client_id)
    if employee_id:
        q = q.where(TimeEntry.employee_id == employee_id)
    return db.scalars(q.order_by(TimeEntry.date.desc(), TimeEntry.id.desc())).all()


@router.get("")
def time_list(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    start = parse_date(qp.get("start"), week_start)
    end = parse_date(qp.get("end"), start + dt.timedelta(days=6))
    client_id = opt_int(qp.get("client_id"))
    employee_id = opt_int(qp.get("employee_id"))
    entries = _filtered(db, start, end, client_id, employee_id)
    by_day: dict[dt.date, int] = defaultdict(int)
    by_project: dict[str, int] = defaultdict(int)
    for e in entries:
        by_day[e.date] += e.minutes
        by_project[e.project.label] += e.minutes
    edit = db.get(TimeEntry, int(qp["edit"])) if qp.get("edit") else None
    span = (end - start).days + 1
    return render(
        request, "time.html", entries=entries, start=start, end=end,
        prev_start=start - dt.timedelta(days=span), next_start=start + dt.timedelta(days=span), span=span,
        projects=_active_projects(db), employees=db.scalars(select(Employee).where(Employee.active.is_(True))).all(),
        clients=db.scalars(select(Client).order_by(Client.name)).all(),
        client_id=client_id, employee_id=employee_id, edit=edit,
        total_minutes=sum(e.minutes for e in entries),
        billable_amount=sum(amount_for_minutes(e.minutes, e.project.effective_rate) for e in entries if e.billable),
        by_day=sorted(by_day.items()), by_project=sorted(by_project.items()),
        running=db.scalars(select(TimeEntry).where(TimeEntry.timer_started_at.is_not(None))).all(),
        last_employee=request.cookies.get("last_employee"),
    )


@router.post("/save")
async def time_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    entry = db.get(TimeEntry, int(f["id"])) if f.get("id") else TimeEntry()
    back = f.get("back") or "/time"
    if entry.invoice_id:
        return redirect(back, err="This entry is on an invoice. Void or edit the invoice first.")
    try:
        entry.date = parse_date(f.get("date"), dt.date.today())
        entry.project_id = int(f["project_id"])
        entry.employee_id = opt_int(f.get("employee_id"))
        if entry.timer_started_at is None:
            entry.minutes = parse_hours(f.get("hours"))
        entry.description = f.get("description", "").strip()
        entry.billable = checked(f, "billable")
    except (ValueError, KeyError) as exc:
        return redirect(back, err=str(exc) if str(exc) != "'project_id'" else "Choose a project")
    db.add(entry)
    db.commit()
    resp = redirect(back, msg=f"Saved {fmt_hours(entry.minutes)} h")
    if entry.employee_id:
        resp.set_cookie("last_employee", str(entry.employee_id), max_age=31536000, samesite="lax")
    return resp


@router.post("/{entry_id}/delete")
async def time_delete(entry_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    back = f.get("back") or "/time"
    entry = db.get(TimeEntry, entry_id)
    if entry and entry.invoice_id:
        return redirect(back, err="This entry is on an invoice and can't be deleted.")
    if entry:
        db.delete(entry)
        db.commit()
    return redirect(back, msg="Entry deleted")


@router.post("/timer/start")
async def timer_start(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    try:
        project_id = int(f["project_id"])
    except (KeyError, ValueError):
        return redirect("/time", err="Choose a project to start a timer")
    entry = TimeEntry(
        date=dt.date.today(), project_id=project_id, employee_id=opt_int(f.get("employee_id")),
        minutes=0, description=f.get("description", "").strip(), billable=True,
        timer_started_at=dt.datetime.now(),
    )
    db.add(entry)
    db.commit()
    return redirect(f.get("back") or "/time", msg="Timer started")


@router.post("/timer/{entry_id}/stop")
async def timer_stop(entry_id: int, request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    entry = db.get(TimeEntry, entry_id)
    if entry and entry.timer_started_at:
        elapsed = (dt.datetime.now() - entry.timer_started_at).total_seconds()
        entry.minutes += max(1, math.ceil(elapsed / 60))
        entry.timer_started_at = None
        db.commit()
        return redirect(f.get("back") or "/time", msg=f"Timer stopped: {fmt_hours(entry.minutes)} h")
    return redirect(f.get("back") or "/time")


@router.get("/export.csv")
def time_export(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    start = parse_date(qp.get("start"), dt.date(dt.date.today().year, 1, 1))
    end = parse_date(qp.get("end"), dt.date.today())
    entries = _filtered(db, start, end, opt_int(qp.get("client_id")), opt_int(qp.get("employee_id")))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Client", "Project", "Employee", "Hours", "Rate", "Amount", "Billable", "Invoiced", "Description"])
    for e in reversed(entries):
        rate = e.project.effective_rate
        w.writerow([
            e.date.isoformat(), e.project.client.name, e.project.name, e.employee.name if e.employee else "",
            fmt_hours(e.minutes), f"{rate / 100:.2f}", f"{amount_for_minutes(e.minutes, rate) / 100:.2f}",
            "Y" if e.billable else "N", "Y" if e.invoice_id else "N", e.description,
        ])
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=time-{start}-{end}.csv"},
    )
