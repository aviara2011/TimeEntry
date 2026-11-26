# TimeEntry

A private, local replacement for QuickBooks Online for a small S-Corp: **time tracking,
client invoicing, payroll, and federal payroll and income tax worksheets**.

It runs on your own computer as a web app (FastAPI + SQLite). All your data is in one file,
`data/timeentry.db`. Nothing is sent anywhere.

## What it does

| Area | Features |
|---|---|
| **Time** | Log hours (`1.5`, `1:30`, `45m`) per client project and per person, start/stop timer, weekly view, filters, CSV export |
| **Clients** | Clients with default hourly rates; projects with optional rate overrides; payment terms |
| **Invoices** | Build an invoice from unbilled time, add fixed-fee lines, draft → sent → paid/void, partial payments, printable / save-as-PDF invoice |
| **Payroll** | Salaried owner-employees on a weekly/biweekly/semimonthly/monthly schedule. Federal income tax withholding (IRS Pub. 15-T percentage method, 2020+ W-4), Social Security with wage base, Medicare and Additional Medicare, FUTA, state unemployment (configurable rate and wage base), 2% shareholder health premiums, pay stubs with YTD |
| **History** | "Historical" pay runs to type in payroll you already ran in QuickBooks, so YTD wage bases, Q4 941 and W-2s are complete |
| **Bank** | Automatic bank feed through SimpleFIN Bridge, or import OFX / QFX / QuickBooks Web Connect (.qbo) / CSV downloads. Review screen that matches deposits to invoices, withdrawals to expenses, payroll and IRS deposits, remembers categories by payee, and records distributions. Statement reconciliation with locking and undo |
| **Expenses** | Business expenses by Form 1120-S line, shareholder distributions, non-deductible items |
| **Taxes** | Form 941 (each quarter, incl. monthly liability schedule), Form 940, W-2 / W-3 worksheets + CSV, Form 1120-S income/deductions with 1125-E officer comp and K-1 allocation, EFTPS deposit log, upcoming-deadline list |

It **prepares the numbers; you file**. Pay deposits through EFTPS, file W-2s with SSA
Business Services Online, and file 941/940/1120-S on paper, through tax software, or via
your CPA. Direct IRS e-filing requires IRS authorization as an e-file provider and isn't included.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m timeentry                                      # then open http://127.0.0.1:8000
```

It only listens on `127.0.0.1`, so other devices can't reach it. To keep the database
somewhere else (for example an encrypted or synced folder), set `TIMEENTRY_DB=/path/to/timeentry.db`.

Run tests with `pytest`.

## First-time setup

1. **Settings**: company name, address, EIN, pay frequency, Wyoming UI rate and taxable
   wage base (from your Wyoming DWS rate notice), cash vs accrual, invoice numbering
   (set "next number" to continue from QuickBooks).
2. **Employees**: add both owners with salary, ownership % (should total 100%), officer
   flag, and their current Form W-4.
3. **Clients & projects** with hourly rates.
4. **Payroll history for 2026**: for each payroll already run in QuickBooks this year,
   create a pay run, tick *Historical*, type in gross pay and the tax amounts from the
   QuickBooks paystub, and finalize. Then record the EFTPS deposits you've made under **Taxes**.
   After that, check that the Q1–Q3 941 worksheets match what you filed.
5. From then on: log time → invoice → record payments → run payroll → deposit → file.

## Bank feeds

Two ways to get transactions in (both can be used side by side; duplicates are skipped):

* **Automatic (SimpleFIN Bridge)**: sign up at SimpleFIN Bridge (about $15/year), connect your
  business bank there, create a setup token, and paste it on the **Bank** page. The app gets
  read-only access; the access key stays in your local database. Click **Sync now** whenever
  you want new transactions (the first sync pulls 90 days).
* **Files**: in your bank's website, download transactions as *Quicken (.qfx)*, *QuickBooks
  Web Connect (.qbo)*, *OFX*, or *CSV*, and import them on the account's page.

Then work through **To review**: click a suggested match or pick a category. Categorizing
creates the invoice payment, expense, distribution or tax deposit for you; **Undo** removes it.
Each month, open **Reconcile**, enter the statement's ending date and balance, untick anything
not on the statement, and finish when the difference is $0.00.

When switching from QuickBooks, set each account's starting balance to the balance on the
day before the first transaction you import, ideally a statement date QuickBooks already
reconciled.

## Important tax notes

* **Tax tables are per year.** `timeentry/tax/tables.py` holds the 2026 numbers. Before your
  first 2027 payroll, add a 2027 entry from the new Pub. 15-T, the SSA wage base and the IRS
  inflation adjustments. The app refuses to run payroll for a year with no tables.
* **2% shareholder health insurance** goes in the paycheck's "shareholder health" field,
  usually on the last payroll of the year. It's added to W-2 box 1 and 941 line 2, and
  is exempt from SS/Medicare/FUTA.
* **Wyoming** has no state income tax. Confirm with Wyoming DWS whether corporate officers
  are covered for unemployment insurance. If they're exempt, tick "Exempt from state
  unemployment" on the employee. Wyoming workers' comp is not calculated.
* **Reasonable compensation**: the IRS expects owner-employees to take a reasonable salary
  before distributions. The 1120-S worksheet warns about distributions with no officer
  pay, or distributions that aren't proportional to ownership.
* This is bookkeeping software, not tax advice. Have your CPA review the first quarter
  you file from it.

## Backups

**Settings → Download backup** saves a copy of the database. It contains SSNs, so store it encrypted.

## Project layout

```
timeentry/
  tax/tables.py        yearly federal parameters (update every January)
  tax/withholding.py   Pub. 15-T Worksheet 1A
  payroll.py           paycheck calculation with YTD wage-base tracking
  reports.py           941, 940, W-2/W-3, 1120-S/K-1, deadlines
  bank_import.py       OFX/QFX/QBO and CSV parsers
  simplefin.py         SimpleFIN Bridge client
  banking.py           import de-duplication, matching, categorizing, reconciliation
  models.py            database tables
  web/                 FastAPI routes, Jinja templates, CSS
tests/                 unit + end-to-end tests
```
