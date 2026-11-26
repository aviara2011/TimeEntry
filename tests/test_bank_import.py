import base64
import datetime as dt

import httpx
import pytest

from timeentry import simplefin
from timeentry.bank_import import parse_csv, parse_file, parse_ofx

OFX_SGML = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20261126120000</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>102000021<ACCTID>123456789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20261101<DTEND>20261126
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261103120000[-7:MST]<TRNAMT>1907.50<FITID>A1<NAME>ACME LOGISTICS ACH<MEMO>INV-1001</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261105<TRNAMT>-48.00<FITID>A2<NAME>GITHUB &amp; CO</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>25000.12<DTASOF>20261126</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

OFX_XML = """<?xml version="1.0"?><?OFX OFXHEADER="200" VERSION="220"?>
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CCACCTFROM><ACCTID>4111000011112222</ACCTID></CCACCTFROM>
<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261110</DTPOSTED><TRNAMT>-120.55</TRNAMT><FITID>X9</FITID><NAME>DELTA AIR</NAME></STMTTRN></BANKTRANLIST>
<LEDGERBAL><BALAMT>-120.55</BALAMT><DTASOF>20261126</DTASOF></LEDGERBAL></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"""


def test_ofx_sgml():
    st = parse_ofx(OFX_SGML)
    assert st.account_number == "123456789"
    assert st.balance == 2500012 and st.balance_date == dt.date(2026, 11, 26)
    a, b = st.transactions
    assert (a.date, a.amount, a.description, a.memo, a.external_id) == (dt.date(2026, 11, 3), 190750, "ACME LOGISTICS ACH", "INV-1001", "ofx:A1")
    assert b.amount == -4800 and b.description == "GITHUB & CO"


def test_ofx_xml_credit_card():
    st = parse_file("card.qfx", OFX_XML.encode())
    assert st.account_number.endswith("2222")
    assert [t.amount for t in st.transactions] == [-12055]


def test_csv_amount_column_with_preamble():
    data = "Account: Business Checking\n\nDate,Description,Amount,Balance\n11/03/2026,ACME LOGISTICS,1907.50,\n11/05/2026,\"GITHUB, INC\",-48.00,\n11/05/2026,\"GITHUB, INC\",-48.00,\nTotal,,1811.50,\n"
    st = parse_csv(data)
    assert [t.amount for t in st.transactions] == [190750, -4800, -4800]
    assert st.transactions[1].external_id != st.transactions[2].external_id  # same-day identical rows kept


def test_csv_debit_credit_and_flip():
    data = "Posting Date,Description,Debit,Credit\n2026-11-03,Deposit,,500.00\n2026-11-04,Office Depot,25.10,\n"
    assert [t.amount for t in parse_csv(data).transactions] == [50000, -2510]
    data2 = "Transaction Date,Description,Amount\n11/04/2026,Delta,120.55\n"
    assert parse_csv(data2, flip_sign=True).transactions[0].amount == -12055


def test_csv_errors():
    with pytest.raises(ValueError):
        parse_csv("foo,bar\n1,2\n")


SF_RESPONSE = {
    "errors": [],
    "accounts": [{
        "org": {"domain": "chase.com", "name": "Chase"}, "id": "ACT-1", "name": "Business Checking",
        "currency": "USD", "balance": "10250.00", "available-balance": "10250.00", "balance-date": 1795000000,
        "transactions": [
            {"id": "T1", "posted": 1794500000, "amount": "-48.00", "description": "GITHUB", "payee": "GitHub", "memo": ""},
            {"id": "T2", "posted": 0, "amount": "-10.00", "description": "PENDING THING", "pending": True, "transacted_at": 1794900000},
        ],
    }],
}


def test_simplefin_parse():
    accounts, errors = simplefin.parse_account_set(SF_RESPONSE)
    a = accounts[0]
    assert (a.id, a.institution, a.balance) == ("ACT-1", "Chase", 1025000)
    assert a.transactions[0].amount == -4800 and a.transactions[0].description == "GitHub"
    assert a.transactions[0].memo == "GITHUB" and a.transactions[0].external_id == "sf:T1"
    assert a.transactions[1].pending
    assert errors == []


def test_simplefin_claim_and_fetch():
    claim_url = "https://bridge.example/simplefin/claim/abc"
    token = base64.b64encode(claim_url.encode()).decode()
    seen = {}

    def handler(request: httpx.Request):
        if request.method == "POST":
            assert str(request.url) == claim_url
            return httpx.Response(200, text="https://user:secret@bridge.example/simplefin")
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=SF_RESPONSE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    access = simplefin.claim_setup_token(token, client=client)
    assert access == "https://user:secret@bridge.example/simplefin"
    accounts, _ = simplefin.fetch_accounts(access, dt.date(2026, 9, 1), client=client)
    assert seen["auth"] == "Basic " + base64.b64encode(b"user:secret").decode()
    assert seen["url"].startswith("https://bridge.example/simplefin/accounts?start-date=")
    assert "user" not in seen["url"]
    assert len(accounts) == 1
    with pytest.raises(ValueError):
        simplefin.claim_setup_token("not a token", client=client)
