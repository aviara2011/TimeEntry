"""Build a demo company so the app can be explored without real data.

    python -m timeentry --demo      (uses data/demo.db; your real data is untouched)
"""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

from fastapi.testclient import TestClient

from .bank_import import ParsedTxn
from .banking import apply_action, import_transactions, suggestions
from .coa import by_code
from .models import BankAccount, BankTransaction, Invoice, Paycheck, PayRun

YEAR = 2026


def _month_end(m: int) -> dt.date:
    return (dt.date(YEAR, m + 1, 1) if m < 12 else dt.date(YEAR + 1, 1, 1)) - dt.timedelta(days=1)


def build_demo(path: Path, today: dt.date | None = None) -> None:
    from .web.app import create_app

    today = today or dt.date.today()
    rnd = random.Random(7)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    app = create_app(f"sqlite:///{path}")
    c = TestClient(app)
    db = app.state.sessionmaker()

    def post(url, data=None, **kw):
        r = c.post(url, data=data or {}, **kw)
        assert r.status_code == 200 and 'flash err' not in r.text, (url, r.text[:300])
        return r

    post("/settings", {"name": "Rocky Peak Consulting, Inc.", "ein": "83-1234567",
                       "address": "1712 Capitol Ave, Suite 200\nCheyenne, WY 82001", "phone": "(307) 555-0142",
                       "email": "billing@rockypeak.example", "pay_frequency": "monthly", "state": "WY",
                       "state_account_no": "WY-0045123", "suta_rate_pct": "1.0", "suta_wage_base": "33800",
                       "accounting_method": "cash", "invoice_prefix": "RP-", "next_invoice_number": "2026001",
                       "payment_terms_days": "30",
                       "invoice_footer": "Please pay by ACH to Chase account ending 4821, routing 102001017.\nThank you for your business."})
    post("/employees/save", {"first_name": "Alex", "last_name": "Rivera", "ssn": "123-45-6789",
                             "address": "410 Pioneer Ave\nCheyenne, WY 82001", "annual_salary": "96000",
                             "ownership_pct": "50", "is_officer": "on", "filing_status": "mfj", "active": "on"})
    post("/employees/save", {"first_name": "Sam", "last_name": "Chen", "ssn": "987-65-4321",
                             "address": "88 Ridge Rd\nLaramie, WY 82070", "annual_salary": "84000",
                             "ownership_pct": "50", "is_officer": "on", "filing_status": "single", "active": "on"})
    post("/clients/save", {"name": "Acme Logistics", "contact": "Dana Ortiz", "email": "ap@acme.example",
                           "address": "1 Dock Rd\nDenver, CO 80216", "default_rate": "165", "active": "on"})
    post("/clients/save", {"name": "Bluebird Health", "contact": "Priya Shah", "email": "finance@bluebird.example",
                           "address": "900 Aspen Way\nBoulder, CO 80301", "default_rate": "180", "terms_days": "15", "active": "on"})
    post("/projects/save", {"client_id": "1", "name": "Data platform"})
    post("/projects/save", {"client_id": "1", "name": "Support retainer", "rate": "140"})
    post("/projects/save", {"client_id": "2", "name": "Patient portal"})
    post("/bank/accounts/save", {"name": "Chase Business Checking", "institution": "Chase", "last4": "4821",
                                 "opening_balance": "42000", "opening_date": f"{YEAR - 1}-12-31", "kind": "checking"})

    tasks = {1: ["Pipeline design", "ETL jobs", "Schema migration", "Client workshop", "Data quality checks"],
             2: ["On-call", "Incident follow-up", "Monthly maintenance"],
             3: ["Portal UX review", "API integration", "Sprint planning", "Accessibility fixes"]}
    bank: list[ParsedTxn] = []
    fid = 0

    def txn(date, amount, name):
        nonlocal fid
        fid += 1
        bank.append(ParsedTxn(date=date, amount=amount, description=name, external_id=f"demo:{fid}"))

    last_full_month = today.month - 1 if today.year == YEAR else 12
    for m in range(1, (today.month if today.year == YEAR else 12) + 1):
        # Time: a few entries per working day
        d = dt.date(YEAR, m, 1)
        while d.month == m and d <= today:
            if d.weekday() < 5:
                for emp in (1, 2):
                    project = rnd.choice([1, 1, 2, 3, 3])
                    post("/time/save", {"date": d.isoformat(), "project_id": str(project), "employee_id": str(emp),
                                        "hours": str(rnd.choice([2, 3, 3.5, 4, 5, 6])),
                                        "description": rnd.choice(tasks[project]), "billable": "on"})
            d += dt.timedelta(days=1)
        if m > last_full_month:
            for day, amt, name in ((3, -5400, "GITHUB INC SAN FRANCISCO CA"), (5, -8999, "VERIZON WIRELESS PAYMENTS")):
                if dt.date(YEAR, m, day) <= today:
                    txn(dt.date(YEAR, m, day), amt, name)
            liab = sum(pc.liability_941 for pc in db.query(Paycheck).join(PayRun).filter(PayRun.pay_date == _month_end(m - 1)))
            if liab and dt.date(YEAR, m, 15) <= today:
                txn(dt.date(YEAR, m, 15), -liab, "IRS USATAXPYMT PPD ID: 3387702000")
            continue
        # Invoice each client for the month on the 1st of next month
        for client_id in (1, 2):
            entries = c.get(f"/invoices/new?client_id={client_id}&start={YEAR}-{m:02d}-01&end={_month_end(m)}")
            ids = [s.split('"')[0] for s in entries.text.split('name="entry_ids" value="')[1:]]
            issue = _month_end(m) + dt.timedelta(days=1)
            if issue > today:
                continue
            post("/invoices/create", {"client_id": str(client_id), "issue_date": issue.isoformat(), "entry_ids": ids})
            inv = db.query(Invoice).order_by(Invoice.id.desc()).first()
            post(f"/invoices/{inv.id}/status", {"action": "send"})
            paid = issue + dt.timedelta(days=rnd.randint(12, 28))
            if paid < today - dt.timedelta(days=10):
                db.expire_all()
                total = db.get(Invoice, inv.id).total
                name = "ACME LOGISTICS PPD ID: 8411907755" if client_id == 1 else "BLUEBIRD HEALTH ACH CREDIT"
                txn(paid, total, name)
        # Payroll on the last day of the month
        if _month_end(m) < today:
            post("/payroll/create", {"period_start": f"{YEAR}-{m:02d}-01", "period_end": _month_end(m).isoformat(),
                                     "pay_date": _month_end(m).isoformat()})
            run = db.query(PayRun).order_by(PayRun.id.desc()).first()
            post(f"/payroll/{run.id}/finalize")
            db.expire_all()
            for pc in db.query(Paycheck).filter(Paycheck.pay_run_id == run.id):
                txn(_month_end(m), -pc.net, f"PAYROLL DIRECT DEP {pc.employee.last_name.upper()}")
        # Monthly 941 deposit on the 15th (for last month)
        if m > 1:
            liab = sum(pc.liability_941 for pc in db.query(Paycheck).join(PayRun).filter(PayRun.pay_date == _month_end(m - 1)))
            if liab:
                txn(dt.date(YEAR, m, 15), -liab, "IRS USATAXPYMT PPD ID: 3387702000")
        # Recurring spending
        txn(dt.date(YEAR, m, 3), -5400, "GITHUB INC SAN FRANCISCO CA")
        txn(dt.date(YEAR, m, 5), -8999, "VERIZON WIRELESS PAYMENTS")
        txn(dt.date(YEAR, m, 8), -2999, "GOOGLE *WORKSPACE")
        txn(dt.date(YEAR, m, 12), -rnd.randint(4000, 30000), "AMAZON MKTPL OFFICE SUPPLIES")
        if m in (3, 6, 9):
            txn(dt.date(YEAR, m, 28), -1500000, "ONLINE TRANSFER TO RIVERA")
            txn(dt.date(YEAR, m, 28), -1500000, "ONLINE TRANSFER TO CHEN")
        if m == 2:
            txn(dt.date(YEAR, 2, 20), -185000, "HARTFORD INSURANCE PREM")
        if m == 4:
            txn(dt.date(YEAR, 4, 10), -125000, "MOUNTAIN CPA GROUP")
            txn(dt.date(YEAR, 4, 30), -6200, "WYOMING DWS UI TAX")
        if m in (5, 9):
            txn(dt.date(YEAR, m, 18), -rnd.randint(40000, 70000), "UNITED AIRLINES")
            txn(dt.date(YEAR, m, 19), -rnd.randint(6000, 15000), "THE BREWERY RESTAURANT")
        if m == 7:
            txn(dt.date(YEAR, 7, 22), -249900, "APPLE STORE R123")
    # A few things this month that haven't happened in the books yet
    txn(today - dt.timedelta(days=6), -3200, "ADOBE *CREATIVE CLD")
    txn(today - dt.timedelta(days=4), -2999, "GOOGLE *WORKSPACE")

    acct = db.query(BankAccount).one()
    import_transactions(db, acct, bank, "file")
    from .banking import book_balance
    acct.bank_balance, acct.bank_balance_date = book_balance(db, acct), today
    db.commit()

    plan = {
        "GITHUB": "software", "VERIZON": "phone_internet", "GOOGLE": "software", "AMAZON": "office",
        "HARTFORD": "insurance", "MOUNTAIN CPA": "professional", "WYOMING DWS": "taxes_licenses",
        "UNITED": "travel", "BREWERY": "meals", "APPLE": "equipment",
    }
    cutoff = today - dt.timedelta(days=13)
    for t in db.query(BankTransaction).filter(BankTransaction.status == "new").order_by(BankTransaction.date):
        if t.date > cutoff:
            continue  # leave the latest ones for review
        sug = suggestions(db, t)
        key = None
        if "TRANSFER TO" in t.description:
            key = "dist:1" if "RIVERA" in t.description else "dist:2"
        elif "IRS" in t.description:
            last = t.date.replace(day=1) - dt.timedelta(days=1)
            key = f"tax941:{last.year}:{(last.month - 1) // 3 + 1}"
        else:
            key = next((s.key for s in sug if s.key.startswith(("inv:", "link:paycheck"))), None)
            if key is None:
                code = next((v for k, v in plan.items() if k in t.description), None)
                key = f"acct:{by_code(db, code).id}" if code else None
        if key:
            apply_action(db, t, key)
            db.commit()

    # Reconcile through the end of the month before last
    rec_end = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1) - dt.timedelta(days=1)
    db.expire_all()
    stmt = book_balance(db, acct, rec_end)
    post("/bank/1/reconcile/start", {"statement_date": rec_end.isoformat(), "statement_balance": f"{stmt / 100:.2f}"})
    ids = [str(t.id) for t in db.query(BankTransaction).filter(BankTransaction.date <= rec_end)]
    post("/bank/reconcile/1/save", {"cleared": ids, "finish": "1"})
    # A draft payroll for the current month and an unpaid invoice make the screens realistic
    post("/payroll/create", {"period_start": today.replace(day=1).isoformat(), "period_end": _month_end(today.month).isoformat(),
                             "pay_date": _month_end(today.month).isoformat()})
    # Depreciation on the laptop
    post("/books/journal/save", {"date": rec_end.isoformat(), "memo": "Laptop depreciation (Section 179 per CPA)",
                                 "account_id": [str(by_code(db, "depreciation").id), str(by_code(db, "accum_depr").id)],
                                 "debit": ["2499.00", ""], "credit": ["", "2499.00"], "line_memo": ["", ""]})
    db.close()
