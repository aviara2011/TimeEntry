"""Dashboard, clients & projects, employees, expenses, settings."""
from __future__ import annotations

import datetime as dt
import tempfile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import EXPENSE_CATEGORIES, PAY_FREQUENCIES, Client, Employee, Expense, Invoice, Payment, Project, TimeEntry
from ..money import amount_for_minutes, parse_money
from ..reports import upcoming_deadlines
from .common import checked, get_company, get_db, opt_int, parse_date, redirect, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    company = get_company(db)
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_minutes = db.scalar(select(func.coalesce(func.sum(TimeEntry.minutes), 0)).where(TimeEntry.date >= week_start))
    unbilled = db.scalars(
        select(TimeEntry).where(TimeEntry.billable.is_(True), TimeEntry.invoice_id.is_(None), TimeEntry.timer_started_at.is_(None))
    ).all()
    unbilled_amount = sum(amount_for_minutes(e.minutes, e.project.effective_rate) for e in unbilled)
    open_invoices = db.scalars(select(Invoice).where(Invoice.status == "sent")).all()
    ar = sum(i.balance for i in open_invoices)
    overdue = [i for i in open_invoices if i.due_date < today]
    ytd_receipts = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.date >= dt.date(today.year, 1, 1))
    )
    running = db.scalars(select(TimeEntry).where(TimeEntry.timer_started_at.is_not(None))).all()
    return render(
        request, "dashboard.html", company=company, week_minutes=week_minutes,
        unbilled_minutes=sum(e.minutes for e in unbilled), unbilled_amount=unbilled_amount,
        ar=ar, overdue=overdue, ytd_receipts=ytd_receipts, running=running,
        deadlines=upcoming_deadlines(today, company),
        setup_needed=not company.ein or not db.scalar(select(func.count(Employee.id))),
    )


# ------------------------------------------------------------------ clients
@router.get("/clients")
def clients(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Client).order_by(Client.active.desc(), Client.name)).all()
    return render(request, "clients.html", clients=rows)


@router.get("/clients/new")
@router.get("/clients/{client_id}")
def client_form(request: Request, client_id: int | None = None, db: Session = Depends(get_db)):
    client = db.get(Client, client_id) if client_id else Client(default_rate=0, active=True)
    return render(request, "client_form.html", client=client)


@router.post("/clients/save")
async def client_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    client = db.get(Client, int(f["id"])) if f.get("id") else Client()
    try:
        client.name = f["name"].strip()
        if not client.name:
            raise ValueError("Client name is required")
        client.contact = f.get("contact", "")
        client.email = f.get("email", "")
        client.address = f.get("address", "")
        client.default_rate = parse_money(f.get("default_rate"))
        client.terms_days = opt_int(f.get("terms_days"))
        client.active = checked(f, "active")
    except (ValueError, KeyError) as exc:
        return redirect(f"/clients/{client.id or 'new'}", err=str(exc))
    db.add(client)
    db.commit()
    return redirect(f"/clients/{client.id}", msg="Client saved")


@router.post("/projects/save")
async def project_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    project = db.get(Project, int(f["id"])) if f.get("id") else Project(client_id=int(f["client_id"]))
    name = f.get("name", "").strip()
    if not name:
        return redirect(f"/clients/{project.client_id}", err="Project name is required")
    project.name = name
    project.rate = parse_money(f["rate"]) if f.get("rate") else None
    project.active = checked(f, "active") if f.get("id") else True
    db.add(project)
    db.commit()
    return redirect(f"/clients/{project.client_id}", msg="Project saved")


# ------------------------------------------------------------------ employees
@router.get("/employees")
def employees(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Employee).order_by(Employee.active.desc(), Employee.last_name)).all()
    return render(request, "employees.html", employees=rows, company=get_company(db))


@router.get("/employees/new")
@router.get("/employees/{employee_id}")
def employee_form(request: Request, employee_id: int | None = None, db: Session = Depends(get_db)):
    emp = db.get(Employee, employee_id) if employee_id else Employee(
        first_name="", last_name="", is_officer=True, ownership_pct="0", annual_salary=0,
        filing_status="single", w4_step2=False, w4_step3=0, w4_step4a=0, w4_step4b=0, w4_step4c=0,
        w4_exempt=False, suta_exempt=False, active=True, ssn="", address="",
    )
    return render(request, "employee_form.html", emp=emp)


@router.post("/employees/save")
async def employee_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    emp = db.get(Employee, int(f["id"])) if f.get("id") else Employee()
    try:
        emp.first_name = f["first_name"].strip()
        emp.last_name = f["last_name"].strip()
        if not emp.first_name or not emp.last_name:
            raise ValueError("First and last name are required")
        emp.ssn = f.get("ssn", "").strip() or (emp.ssn or "")
        emp.address = f.get("address", "")
        emp.is_officer = checked(f, "is_officer")
        pct = f.get("ownership_pct", "0").strip() or "0"
        float(pct)
        emp.ownership_pct = pct
        emp.annual_salary = parse_money(f.get("annual_salary"))
        emp.filing_status = f.get("filing_status", "single")
        emp.w4_step2 = checked(f, "w4_step2")
        emp.w4_step3 = parse_money(f.get("w4_step3"))
        emp.w4_step4a = parse_money(f.get("w4_step4a"))
        emp.w4_step4b = parse_money(f.get("w4_step4b"))
        emp.w4_step4c = parse_money(f.get("w4_step4c"))
        emp.w4_exempt = checked(f, "w4_exempt")
        emp.suta_exempt = checked(f, "suta_exempt")
        emp.active = checked(f, "active")
    except (ValueError, KeyError) as exc:
        return redirect(f"/employees/{emp.id or 'new'}", err=str(exc))
    db.add(emp)
    db.commit()
    return redirect("/employees", msg=f"Saved {emp.name}")


# ------------------------------------------------------------------ expenses
@router.get("/expenses")
def expenses(request: Request, year: int | None = None, db: Session = Depends(get_db)):
    year = year or dt.date.today().year
    rows = db.scalars(
        select(Expense).where(Expense.date >= dt.date(year, 1, 1), Expense.date <= dt.date(year, 12, 31))
        .order_by(Expense.date.desc(), Expense.id.desc())
    ).all()
    totals: dict[str, int] = {}
    for e in rows:
        totals[e.category] = totals.get(e.category, 0) + e.amount
    holders = db.scalars(select(Employee).order_by(Employee.last_name)).all()
    edit = db.get(Expense, int(request.query_params["edit"])) if request.query_params.get("edit") else None
    return render(request, "expenses.html", expenses=rows, year=year, totals=totals, holders=holders, edit=edit)


@router.post("/expenses/save")
async def expense_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    exp = db.get(Expense, int(f["id"])) if f.get("id") else Expense()
    try:
        exp.date = parse_date(f.get("date"), dt.date.today())
        exp.payee = f.get("payee", "")
        exp.category = f.get("category", "other")
        if exp.category not in EXPENSE_CATEGORIES:
            raise ValueError("Unknown category")
        exp.amount = parse_money(f.get("amount"))
        if exp.amount <= 0:
            raise ValueError("Amount must be greater than zero")
        exp.memo = f.get("memo", "")
        exp.shareholder_id = opt_int(f.get("shareholder_id"))
    except ValueError as exc:
        return redirect("/expenses", err=str(exc))
    db.add(exp)
    db.commit()
    return redirect(f"/expenses?year={exp.date.year}", msg="Expense saved")


@router.post("/expenses/{expense_id}/delete")
def expense_delete(expense_id: int, db: Session = Depends(get_db)):
    exp = db.get(Expense, expense_id)
    if exp:
        db.delete(exp)
        db.commit()
    return redirect("/expenses", msg="Expense deleted")


# ------------------------------------------------------------------ settings
@router.get("/settings")
def settings(request: Request, db: Session = Depends(get_db)):
    return render(request, "settings.html", company=get_company(db))


@router.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    c = get_company(db)
    try:
        c.name = f.get("name", "").strip() or c.name
        c.address = f.get("address", "")
        c.phone = f.get("phone", "")
        c.email = f.get("email", "")
        c.ein = f.get("ein", "")
        c.state = (f.get("state", "WY") or "WY").upper()[:2]
        c.state_account_no = f.get("state_account_no", "")
        if f.get("pay_frequency") not in PAY_FREQUENCIES:
            raise ValueError("Unknown pay frequency")
        c.pay_frequency = f["pay_frequency"]
        rate = (f.get("suta_rate_pct") or "0").replace("%", "").strip()
        float(rate)
        c.suta_rate_pct = rate
        c.suta_wage_base = parse_money(f.get("suta_wage_base"))
        c.accounting_method = "accrual" if f.get("accounting_method") == "accrual" else "cash"
        c.invoice_prefix = f.get("invoice_prefix", "")
        c.next_invoice_number = int(f.get("next_invoice_number") or c.next_invoice_number)
        c.payment_terms_days = int(f.get("payment_terms_days") or 30)
        c.invoice_footer = f.get("invoice_footer", "")
    except ValueError as exc:
        return redirect("/settings", err=str(exc))
    db.commit()
    return redirect("/settings", msg="Settings saved")


@router.get("/backup")
def backup(request: Request):
    """Download a consistent copy of the SQLite database."""
    engine = request.app.state.engine
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    raw = engine.raw_connection()
    try:
        import sqlite3

        dest = sqlite3.connect(tmp.name)
        raw.driver_connection.backup(dest)
        dest.close()
    finally:
        raw.close()
    name = f"timeentry-backup-{dt.datetime.now():%Y%m%d-%H%M}.db"
    return FileResponse(tmp.name, filename=name, media_type="application/octet-stream")


