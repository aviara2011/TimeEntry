import datetime as dt

from timeentry.models import Client, Company, Employee, Expense, Invoice, InvoiceLine, Payment, Paycheck, PayRun, TaxDeposit
from timeentry.payroll import recompute_run
from timeentry.reports import form_940, form_941, form_1120s, w2s


def setup_company(db, **kw):
    c = Company(pay_frequency="monthly", suta_rate_pct="1.0", suta_wage_base=33_000_00, **kw)
    db.add(c)
    a = Employee(first_name="Ann", last_name="Owner", annual_salary=240_000_00, filing_status="mfj",
                 ownership_pct="50", is_officer=True)
    b = Employee(first_name="Bob", last_name="Owner", annual_salary=60_000_00, filing_status="single",
                 ownership_pct="50", is_officer=True)
    db.add_all([a, b])
    db.commit()
    return c, a, b


def run_year(db, company, emps, health_in_dec=0):
    for m in range(1, 13):
        end = (dt.date(2026, m % 12 + 1, 1) if m < 12 else dt.date(2027, 1, 1)) - dt.timedelta(days=1)
        run = PayRun(pay_date=end, period_start=dt.date(2026, m, 1), period_end=end, status="draft")
        db.add(run)
        for e in emps:
            run.paychecks.append(Paycheck(employee=e, gross=e.annual_salary // 12,
                                          shareholder_health=health_in_dec if m == 12 else 0))
        db.flush()
        recompute_run(db, company, run)
        run.status = "final"
        db.commit()


def test_wage_bases_caps_and_w2(db):
    company, a, b = setup_company(db)
    run_year(db, company, [a, b], health_in_dec=12_000_00)
    forms = {w.employee.first_name: w for w in w2s(db, 2026)}
    ann = forms["Ann"]
    assert ann.box3 == 184_500_00                       # SS wage base
    assert ann.box4 == 11_439_00
    assert ann.box5 == 240_000_00
    assert ann.box6 == round(240_000_00 * 0.0145) + 360_00  # + 0.9% over 200k
    assert ann.box1 == 252_000_00                       # includes shareholder health
    assert ann.box14_health == 12_000_00
    bob = forms["Bob"]
    assert bob.box3 == 60_000_00 and bob.box4 == 3_720_00

    f940 = form_940(db, 2026)
    assert f940.line7_futa_wages == 14_000_00
    assert f940.line8_futa_tax == 84_00
    assert sum(f940.quarter_liability) == f940.line12
    assert f940.line4_exempt == 24_000_00

    q = [form_941(db, 2026, n) for n in range(1, 5)]
    assert sum(x.line2_wages for x in q) == sum(w.box1 for w in forms.values())
    assert sum(x.line5a_ss_wages for x in q) == sum(w.box3 for w in forms.values())
    assert sum(x.line3_fit for x in q) == sum(w.box2 for w in forms.values())
    for x in q:
        assert sum(x.month_liability) == x.line12
        assert abs(x.line7_fractions) < 100
        assert x.line1_employees == 2

    # SUTA capped at the configured base
    suta_wages = sum(pc.suta_wages for pc in db.query(Paycheck).filter(Paycheck.employee_id == a.id))
    assert suta_wages == 33_000_00


def test_941_deposits(db):
    company, a, b = setup_company(db)
    run_year(db, company, [a, b])
    q1 = form_941(db, 2026, 1)
    db.add(TaxDeposit(date=dt.date(2026, 2, 15), form="941", year=2026, quarter=1, amount=q1.line12 - 100))
    db.commit()
    q1 = form_941(db, 2026, 1)
    assert q1.line14_balance_due == 100


def test_manual_historical_paycheck_kept(db):
    company, a, b = setup_company(db)
    run = PayRun(pay_date=dt.date(2026, 1, 31), period_start=dt.date(2026, 1, 1), period_end=dt.date(2026, 1, 31))
    db.add(run)
    run.paychecks.append(Paycheck(employee=a, gross=20_000_00, manual=True, fit=3_000_00, ss_ee=1_240_00, medicare_ee=290_00))
    db.flush()
    recompute_run(db, company, run)
    pc = run.paychecks[0]
    assert pc.fit == 3_000_00 and pc.ss_er == 1_240_00 and pc.net == 20_000_00 - 4_530_00


def test_1120s(db):
    company, a, b = setup_company(db, accounting_method="cash")
    run_year(db, company, [a, b])
    client = Client(name="Acme")
    db.add(client)
    db.flush()
    inv = Invoice(number="1", client_id=client.id, issue_date=dt.date(2026, 3, 1), due_date=dt.date(2026, 3, 31), status="paid")
    inv.lines.append(InvoiceLine(description="work", quantity="1", unit_price=500_000_00, amount=500_000_00))
    inv.payments.append(Payment(date=dt.date(2026, 3, 20), amount=500_000_00))
    db.add(inv)
    db.add_all([
        Expense(date=dt.date(2026, 5, 1), category="software", amount=1_000_00),
        Expense(date=dt.date(2026, 5, 1), category="meals", amount=200_00),
        Expense(date=dt.date(2026, 6, 1), category="distribution", amount=50_000_00, shareholder_id=a.id),
        Expense(date=dt.date(2026, 6, 1), category="distribution", amount=50_000_00, shareholder_id=b.id),
    ])
    db.commit()
    f = form_1120s(db, company, 2026)
    assert f.line1a_gross_receipts == 500_000_00
    assert f.lines["7"] == 300_000_00
    payroll_taxes = sum(pc.employer_taxes for pc in db.query(Paycheck))
    assert f.lines["12"] == payroll_taxes
    assert f.lines["19"] == 1_000_00 + 100_00
    assert f.line21_ordinary_income == 500_000_00 - f.line20_total_deductions
    assert [k.ordinary_income for k in f.k1s] == [f.line21_ordinary_income // 2] * 2
    assert f.distributions_total == 100_000_00
    assert not f.warnings
