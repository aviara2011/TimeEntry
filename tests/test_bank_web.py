import datetime as dt
import re

from timeentry import simplefin
from timeentry.bank_import import ParsedTxn
from timeentry.models import BankAccount, BankTransaction, Expense, Invoice, Payment, TaxDeposit
from timeentry.simplefin import SFAccount


def ok(r):
    assert r.status_code == 200, r.text[:300]
    m = re.search(r'flash err">([^<]*)', r.text)
    assert not m, m.group(1)
    return r


def txn(db, desc):
    return db.query(BankTransaction).filter(BankTransaction.description == desc).one()


OFX = """<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><ACCTID>99991234</BANKACCTFROM><BANKTRANLIST>
<STMTTRN><DTPOSTED>20261103<TRNAMT>562.50<FITID>1<NAME>ACME CORP ACH PMT</STMTTRN>
<STMTTRN><DTPOSTED>20261105<TRNAMT>-48.00<FITID>2<NAME>GITHUB INC 4471</STMTTRN>
<STMTTRN><DTPOSTED>20261110<TRNAMT>-1200.00<FITID>3<NAME>HARTFORD INSURANCE</STMTTRN>
<STMTTRN><DTPOSTED>20261116<TRNAMT>-2500.00<FITID>4<NAME>IRS USATAXPYMT</STMTTRN>
<STMTTRN><DTPOSTED>20261120<TRNAMT>-5000.00<FITID>5<NAME>ONLINE TRANSFER TO SAM</STMTTRN>
<STMTTRN><DTPOSTED>20261201<TRNAMT>-9.99<FITID>6<NAME>AFTER STATEMENT</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>11794.51<DTASOF>20261201</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""


def test_bank_workflow(client, db):
    ok(client.post("/employees/save", data={"first_name": "Sam", "last_name": "Chen", "ownership_pct": "100",
                                            "is_officer": "on", "filing_status": "single", "active": "on"}))
    ok(client.post("/clients/save", data={"name": "Acme Corp", "default_rate": "150", "active": "on"}))
    ok(client.post("/invoices/create", data={"client_id": "1", "issue_date": "2026-10-01"}))
    ok(client.post("/invoices/1/lines", data={"description": "Work", "quantity": "3.75", "unit_price": "150"}))
    ok(client.post("/invoices/1/status", data={"action": "send"}))
    # An expense typed in by hand before the bank import
    ok(client.post("/expenses/save", data={"date": "2026-11-09", "payee": "Hartford", "category": "insurance", "amount": "1200"}))

    ok(client.post("/bank/accounts/save", data={"name": "Checking", "opening_balance": "20000", "kind": "checking"}))
    r = ok(client.post("/bank/1/import", files={"file": ("nov.qfx", OFX.encode())}))
    assert "Imported 6 new" in r.text
    r = ok(client.post("/bank/1/import", files={"file": ("nov.qfx", OFX.encode())}))
    assert "Imported 0 new transactions (6 already here)" in r.text

    acct = db.get(BankAccount, 1)
    assert acct.last4 == "1234" and acct.bank_balance == 1179451

    # Hand-entered expense was matched automatically (exact amount, close date)
    assert txn(db, "HARTFORD INSURANCE").status == "matched"
    assert db.query(Expense).count() == 1

    r = client.get("/bank/1")
    assert "Payment for INV-1001" in r.text and "Form 941 deposit for 2026 Q4" in r.text

    deposit = txn(db, "ACME CORP ACH PMT")
    ok(client.post(f"/bank/txn/{deposit.id}/action", data={"action": "inv:1"}))
    db.expire_all()
    assert db.get(Invoice, 1).status == "paid"

    gh = txn(db, "GITHUB INC 4471")
    ok(client.post(f"/bank/txn/{gh.id}/action", data={"action": "exp:software"}))
    irs = txn(db, "IRS USATAXPYMT")
    ok(client.post(f"/bank/txn/{irs.id}/action", data={"action": "tax941:2026:4"}))
    sam = txn(db, "ONLINE TRANSFER TO SAM")
    ok(client.post(f"/bank/txn/{sam.id}/action", data={"action": "dist:1"}))
    db.expire_all()
    assert db.query(TaxDeposit).one().amount == 250000
    assert db.query(Expense).filter(Expense.category == "distribution").one().shareholder_id == 1

    # Undo removes the record the review screen created
    ok(client.post(f"/bank/txn/{gh.id}/unmatch"))
    db.expire_all()
    assert db.query(Expense).filter(Expense.category == "software").count() == 0
    ok(client.post(f"/bank/txn/{gh.id}/action", data={"action": "exp:software"}))

    # Next month's GitHub charge remembers the category
    ok(client.post("/bank/1/import", files={"file": ("dec.csv", b"Date,Description,Amount\n12/05/2026,GITHUB INC 5512,-48.00\n")}))
    assert "Software &amp; subscriptions (same as last time)" in client.get("/bank/1").text

    # Undoing an invoice payment reopens the invoice
    ok(client.post(f"/bank/txn/{deposit.id}/unmatch"))
    db.expire_all()
    assert db.get(Invoice, 1).status == "sent" and db.query(Payment).count() == 0
    ok(client.post(f"/bank/txn/{deposit.id}/action", data={"action": "inv:1"}))

    # Reconcile November: 20000 + 562.50 - 48 - 1200 - 2500 - 5000 = 11814.50
    ok(client.post("/bank/1/reconcile/start", data={"statement_date": "2026-11-30", "statement_balance": "11814.50"}))
    r = ok(client.get("/bank/1/reconcile"))
    assert "AFTER STATEMENT" not in r.text
    ids = [str(t.id) for t in db.query(BankTransaction).filter(BankTransaction.date <= dt.date(2026, 11, 30))]
    r = client.post("/bank/reconcile/1/save", data={"cleared": ids[:-1], "finish": "1"}, follow_redirects=True)
    assert "Off by" in r.text
    ok(client.post("/bank/reconcile/1/save", data={"cleared": ids, "finish": "1"}))
    r = ok(client.get("/bank"))
    assert "Nov 30, 2026" in r.text
    # Reconciled transactions are locked
    r = client.post(f"/bank/txn/{gh.id}/action", data={"action": "exp:office"}, follow_redirects=True)
    assert "completed reconciliation" in r.text

    # Next reconciliation starts from the reconciled balance
    r = ok(client.get("/bank/1/reconcile"))
    assert 'value="11814.50"' in r.text
    assert client.get("/").status_code == 200


def test_simplefin_sync(client, db, monkeypatch):
    monkeypatch.setattr(simplefin, "claim_setup_token", lambda token: "https://u:p@bridge.example/simplefin")
    calls = []

    def fake_fetch(url, start, end=None):
        calls.append(start)
        return [SFAccount(id="ACT-1", name="Business Checking", institution="Chase", currency="USD", balance=1000000,
                          balance_date=dt.date(2026, 11, 26), transactions=[
                              ParsedTxn(dt.date(2026, 11, 20), -4800, "GitHub", external_id="sf:T1"),
                              ParsedTxn(dt.date(2026, 11, 25), -1000, "Pending", external_id="sf:T2", pending=True),
                          ])], []

    monkeypatch.setattr(simplefin, "fetch_accounts", fake_fetch)
    r = ok(client.post("/bank/simplefin/connect", data={"token": "x"}))
    assert "Synced 1 account(s), 2 new" in r.text
    a = db.query(BankAccount).one()
    assert a.simplefin_id == "ACT-1" and a.opening_balance == 1004800
    r = ok(client.get("/bank/1"))
    assert "$10,000.00" in r.text  # books agree with bank
    r = ok(client.post("/bank/simplefin/sync"))
    assert "0 new" in r.text and len(calls) == 2
