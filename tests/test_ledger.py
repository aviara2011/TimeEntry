import datetime as dt
import re

from sqlalchemy import create_engine, text

from timeentry.bank_import import parse_csv
from timeentry.coa import by_code
from timeentry.db import init_db, make_sessionmaker
from timeentry.ledger import Books, balance_sheet, profit_and_loss, trial_balance
from timeentry.models import Account, BankAccount, BankTransaction, Expense, Invoice

CHASE_CSV = """Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
CREDIT,11/24/2026,"ACME LOGISTICS       PPD ID: 1234567890",1907.50,ACH_CREDIT,21159.93,,
DEBIT,11/16/2026,"IRS      USATAXPYMT   PPD ID: 3387702000",-3695.84,ACH_DEBIT,19252.43,,
CHECK,11/10/2026,"CHECK 1043",-250.00,CHECK_PAID,22948.27,1043,
"""


def ok(r):
    assert r.status_code == 200, r.text[:300]
    m = re.search(r'flash err">([^<]*)', r.text)
    assert not m, m.group(1)
    return r


def test_chase_csv():
    st = parse_csv(CHASE_CSV)
    assert [(t.date, t.amount) for t in st.transactions] == [
        (dt.date(2026, 11, 24), 190750), (dt.date(2026, 11, 16), -369584), (dt.date(2026, 11, 10), -25000)]
    assert st.transactions[0].description.startswith("ACME LOGISTICS")
    assert st.transactions[2].memo == "1043"


OFX = """<OFX><BANKTRANLIST>
<STMTTRN><DTPOSTED>20260210<TRNAMT>5000.00<FITID>1<NAME>ACME PAYMENT</STMTTRN>
<STMTTRN><DTPOSTED>20260131<TRNAMT>-{net}<FITID>2<NAME>PAYROLL SAM CHEN</STMTTRN>
<STMTTRN><DTPOSTED>20260213<TRNAMT>-{tax}<FITID>3<NAME>IRS USATAXPYMT</STMTTRN>
<STMTTRN><DTPOSTED>20260215<TRNAMT>-99.00<FITID>4<NAME>ADOBE SOFTWARE</STMTTRN>
<STMTTRN><DTPOSTED>20260220<TRNAMT>10000.00<FITID>5<NAME>TRANSFER FROM SAM PERSONAL</STMTTRN>
<STMTTRN><DTPOSTED>20260225<TRNAMT>-3000.00<FITID>6<NAME>TRANSFER TO SAM</STMTTRN>
</BANKTRANLIST></OFX>"""


def test_full_books_balance(client, db):
    ok(client.post("/settings", data={"name": "Co", "pay_frequency": "monthly", "suta_rate_pct": "1", "suta_wage_base": "33000",
                                      "state": "WY", "accounting_method": "cash", "next_invoice_number": "1", "payment_terms_days": "30"}))
    ok(client.post("/employees/save", data={"first_name": "Sam", "last_name": "Chen", "annual_salary": "60000",
                                            "ownership_pct": "100", "is_officer": "on", "filing_status": "single", "active": "on"}))
    ok(client.post("/clients/save", data={"name": "Acme", "default_rate": "100", "active": "on"}))
    ok(client.post("/invoices/create", data={"client_id": "1", "issue_date": "2026-01-15"}))
    ok(client.post("/invoices/1/lines", data={"description": "Jan work", "quantity": "60", "unit_price": "100"}))
    ok(client.post("/invoices/1/status", data={"action": "send"}))  # $6,000; $5,000 paid below
    ok(client.post("/payroll/create", data={"period_start": "2026-01-01", "period_end": "2026-01-31", "pay_date": "2026-01-31"}))
    ok(client.post("/payroll/1/finalize"))
    from timeentry.models import Paycheck
    check = db.query(Paycheck).one()
    ok(client.post("/bank/accounts/save", data={"name": "Chase Checking", "institution": "Chase", "last4": "4821",
                                                 "opening_balance": "20000", "opening_date": "2025-12-31", "kind": "checking"}))
    ofx = OFX.format(net=f"{check.net / 100:.2f}", tax=f"{check.liability_941 / 100:.2f}")
    ok(client.post("/bank/1/import", files={"file": ("jan.qfx", ofx.encode())}))
    db.expire_all()

    def t(desc):
        return db.query(BankTransaction).filter(BankTransaction.description == desc).one()

    assert f"link:paycheck:{check.id}" in client.get("/bank/1").text  # suggested, not auto-applied
    ok(client.post(f"/bank/txn/{t('PAYROLL SAM CHEN').id}/action", data={"action": f"link:paycheck:{check.id}"}))
    ok(client.post(f"/bank/txn/{t('ACME PAYMENT').id}/action", data={"action": "inv:1"}))
    ok(client.post(f"/bank/txn/{t('IRS USATAXPYMT').id}/action", data={"action": "tax941:2026:1"}))
    ok(client.post(f"/bank/txn/{t('ADOBE SOFTWARE').id}/action", data={"action": f"acct:{by_code(db, 'software').id}"}))
    ok(client.post(f"/bank/txn/{t('TRANSFER FROM SAM PERSONAL').id}/action", data={"action": f"acct:{by_code(db, 'apic').id}"}))
    ok(client.post(f"/bank/txn/{t('TRANSFER TO SAM').id}/action", data={"action": "dist:1"}))
    # Year-end depreciation via journal entry
    ok(client.post("/books/journal/save", data={"date": "2026-12-31", "memo": "Depreciation",
                                                "account_id": [str(by_code(db, "depreciation").id), str(by_code(db, "accum_depr").id)],
                                                "debit": ["500", ""], "credit": ["", "500"], "line_memo": ["", ""]}))
    db.expire_all()

    for basis in ("cash", "accrual"):
        books = Books(db, basis)
        rows = trial_balance(books, dt.date(2026, 12, 31))
        assert sum(d for _, d, _ in rows) == sum(c for _, _, c in rows)
        bs = balance_sheet(books, dt.date(2026, 12, 31))
        assert bs["balanced"], basis
        bal = books.balances()
        checking = db.query(BankAccount).one().gl_account_id
        bank_total = 2000000 + sum(x.amount for x in db.query(BankTransaction))
        assert bal[checking] == bank_total  # books agree with the bank
        assert bal[by_code(db, "fed_payroll_liab").id] == 0  # deposit paid it off
        pl = profit_and_loss(books, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        revenue = dict((a.code, v) for a, v in pl["income"].rows)["revenue"]
        assert revenue == (500000 if basis == "cash" else 600000)
        if basis == "accrual":
            assert bal[by_code(db, "ar").id] == 100000
        exp = dict((a.code, v) for a, v in pl["expenses"].rows)
        assert exp["officer_comp"] == 500000 and exp["software"] == 9900 and exp["depreciation"] == 50000
        assert exp["payroll_tax"] == check.employer_taxes

    # Next year: last year's income closes into retained earnings, distributions too
    bs27 = balance_sheet(Books(db, "cash"), dt.date(2027, 1, 31))
    assert bs27["balanced"] and bs27["net_income"] == 0
    retained = dict((a.code, v) for a, v in bs27["equity"].rows)["retained"]
    pl26 = profit_and_loss(Books(db, "cash"), dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert retained == pl26["net"] - 300000

    # 1120-S built from the books
    r = ok(client.get("/taxes/1120s?year=2026"))
    assert "$5,000.00" in r.text  # gross receipts (cash)
    # Pages render
    for url in ["/books", "/books/accounts", f"/books/accounts/{by_code(db, 'software').id}", "/books/pl",
                "/books/pl?basis=accrual&format=csv", "/books/balance-sheet", "/books/trial-balance",
                "/books/journal", "/books/journal/1", "/books/general-ledger.csv", "/expenses"]:
        ok(client.get(url))


def test_chart_of_accounts_editing(client, db):
    r = ok(client.post("/books/accounts/save", data={"number": "6605", "name": "Cloud hosting", "type": "expense", "tax_line": "19"}))
    acct = db.query(Account).filter(Account.number == "6605").one()
    assert "Cloud hosting" in client.get("/books/accounts").text
    r = client.post("/books/accounts/save", data={"number": "6600", "name": "Dup", "type": "expense"}, follow_redirects=True)
    assert "already used" in r.text
    sys_acct = by_code(db, "ar")
    r = client.post(f"/books/accounts/{sys_acct.id}/delete", follow_redirects=True)
    assert "can&#39;t be deleted" in r.text or "can't be deleted" in r.text
    ok(client.post("/expenses/save", data={"date": "2026-03-01", "payee": "AWS", "account_id": str(acct.id), "amount": "30"}))
    r = client.post(f"/books/accounts/{acct.id}/delete", follow_redirects=True)
    assert "has transactions" in r.text
    r = client.post("/books/journal/save", data={"date": "2026-01-01", "account_id": [str(acct.id), str(sys_acct.id)],
                                                 "debit": ["10", ""], "credit": ["", "9"], "line_memo": ["", ""]}, follow_redirects=True)
    assert "differ by $1.00" in r.text


def test_migration_adds_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as c:  # the expense table as the first version created it
        c.execute(text("CREATE TABLE expense (id INTEGER PRIMARY KEY, date DATE, payee VARCHAR(200), category VARCHAR(30), "
                       "amount INTEGER, memo TEXT, shareholder_id INTEGER)"))
        c.execute(text("INSERT INTO expense (date, payee, category, amount, memo) VALUES ('2026-01-01', 'X', 'travel', 100, '')"))
    init_db(engine)  # app start on an older database
    with make_sessionmaker(engine)() as db:
        e = db.query(Expense).one()
        assert e.account_id == by_code(db, "travel").id
