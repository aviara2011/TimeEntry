import datetime as dt
import re


def post(client, url, data, follow=True):
    r = client.post(url, data=data, follow_redirects=follow)
    assert r.status_code == 200, r.text[:500]
    assert "flash err" not in r.text, re.search(r'flash err">([^<]*)', r.text).group(1)
    return r


def test_end_to_end(client):
    assert client.get("/").status_code == 200
    for url in ["/clients/new", "/employees/new", "/books/accounts/new", "/books/journal/new"]:
        assert client.get(url).status_code == 200, url
    post(client, "/settings", {"name": "Test Co", "ein": "12-3456789", "pay_frequency": "monthly",
                               "suta_rate_pct": "1", "suta_wage_base": "33000", "state": "WY",
                               "accounting_method": "cash", "invoice_prefix": "INV-",
                               "next_invoice_number": "1001", "payment_terms_days": "30"})
    post(client, "/employees/save", {"first_name": "Ann", "last_name": "Owner", "ssn": "123-45-6789",
                                     "annual_salary": "120000", "ownership_pct": "50", "is_officer": "on",
                                     "filing_status": "mfj", "active": "on"})
    post(client, "/employees/save", {"first_name": "Bob", "last_name": "Owner", "annual_salary": "90000",
                                     "ownership_pct": "50", "is_officer": "on", "filing_status": "single", "active": "on"})
    r = client.get("/employees")
    assert "XXX-XX-6789" in r.text and "123-45-6789" not in r.text

    post(client, "/clients/save", {"name": "Acme Corp", "default_rate": "150", "active": "on"})
    post(client, "/projects/save", {"client_id": "1", "name": "Website"})
    today = dt.date.today().isoformat()
    post(client, "/time/save", {"date": today, "project_id": "1", "employee_id": "1", "hours": "2.5",
                                "description": "Design", "billable": "on"})
    post(client, "/time/save", {"date": today, "project_id": "1", "hours": "1:15", "description": "Calls", "billable": "on"})
    post(client, "/time/timer/start", {"project_id": "1"})
    post(client, "/time/timer/3/stop", {})
    r = client.get("/time")
    assert "Design" in r.text and "$375.00" in r.text
    assert client.get("/time/export.csv").text.count("\n") == 4

    r = client.get("/invoices/new?client_id=1")
    assert "Design" in r.text
    r = post(client, "/invoices/create", {"client_id": "1", "issue_date": today, "entry_ids": ["1", "2"]})
    assert "INV-1001" in r.text and "$562.50" in r.text
    post(client, "/invoices/1/lines", {"description": "Hosting", "quantity": "1", "unit_price": "20"})
    post(client, "/invoices/1/status", {"action": "send"})
    assert "invoiced" in client.get("/time").text
    r = post(client, "/invoices/1/payments", {"date": today, "amount": "582.50", "method": "ACH"})
    assert "badge paid" in r.text
    assert client.get("/invoices/1/print").status_code == 200

    # A sent invoice can't be edited; timer entry remains unbilled
    r = client.get("/invoices/new?client_id=1")
    assert 'value="3"' in r.text

    year = dt.date.today().year
    r = post(client, "/payroll/create", {"period_start": f"{year}-01-01", "period_end": f"{year}-01-31",
                                         "pay_date": f"{year}-01-31"})
    assert 'value="10000.00"' in r.text and "$17,500.00" in r.text  # 120k/12 and total with 90k/12
    post(client, "/payroll/1/finalize", {})
    assert client.get("/payroll/1/stubs").status_code == 200
    r = post(client, "/payroll/create", {"period_start": f"{year}-02-01", "period_end": f"{year}-02-28",
                                         "pay_date": f"{year}-02-28", "historical": "on"})
    post(client, "/payroll/2/save", {"pc3_gross": "10000", "pc3_manual": "on", "pc3_fit": "800", "pc3_ss_ee": "620",
                                     "pc3_medicare_ee": "145", "pc4_gross": "7500", "pc4_manual": "on", "pc4_fit": "600",
                                     "pc4_ss_ee": "465", "pc4_medicare_ee": "108.75"})
    post(client, "/payroll/2/finalize", {})

    r = client.get("/expenses")
    software = re.search(r'<option value="(\d+)" >6600 Software', r.text).group(1)
    post(client, "/expenses/save", {"date": today, "payee": "GitHub", "account_id": software, "amount": "48"})
    post(client, "/taxes/deposits", {"date": today, "form": "941", "year": str(year), "quarter": "1", "amount": "1000"})
    for url in ["/taxes", f"/taxes/941?year={year}&quarter=1", f"/taxes/940?year={year}", f"/taxes/w2?year={year}",
                f"/taxes/1120s?year={year}", f"/taxes/w2.csv?year={year}", "/expenses", "/payroll", "/clients/1", "/settings"]:
        assert client.get(url).status_code == 200, url
    r = client.get(f"/taxes/w2?year={year}")
    assert "$20,000.00" in r.text  # Ann: Jan 10,000 + Feb 10,000
    r = client.get("/backup")
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
