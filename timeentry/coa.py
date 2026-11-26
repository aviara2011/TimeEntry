"""Default chart of accounts for a service S-Corp, and database migration helpers."""
from __future__ import annotations

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .models import Account, BankAccount, Expense

# (number, name, type, code, tax_line, deductible %, system)
DEFAULT_ACCOUNTS = [
    ("1050", "Undeposited funds", "asset", "undeposited", "L1", 100, True),
    ("1090", "Transfers in transit", "asset", "transfers", "L1", 100, False),
    ("1200", "Accounts receivable", "asset", "ar", "L2a", 100, True),
    ("1300", "Prepaid expenses", "asset", "prepaid", "L6", 100, False),
    ("1500", "Computers & equipment", "asset", "equipment", "L10a", 100, False),
    ("1590", "Accumulated depreciation", "asset", "accum_depr", "L10b", 100, False),
    ("2000", "Accounts payable", "liability", "ap", "L16", 100, False),
    ("2100", "Federal payroll taxes payable (941)", "liability", "fed_payroll_liab", "L18", 100, True),
    ("2110", "FUTA payable (940)", "liability", "futa_liab", "L18", 100, True),
    ("2120", "State unemployment payable", "liability", "suta_liab", "L18", 100, True),
    ("2500", "Loans from shareholders", "liability", "shareholder_loans", "L19", 100, False),
    ("2600", "Notes payable", "liability", "notes_payable", "L20", 100, False),
    ("3000", "Common stock", "equity", "stock", "L22", 100, False),
    ("3100", "Additional paid-in capital (owner contributions)", "equity", "apic", "L23", 100, False),
    ("3200", "Retained earnings", "equity", "retained", "L24", 100, True),
    ("3300", "Shareholder distributions", "equity", "distribution", "K16d", 100, True),
    ("3900", "Opening balance equity", "equity", "opening", "L24", 100, True),
    ("4000", "Consulting revenue", "income", "revenue", "1a", 100, True),
    ("4100", "Reimbursed expenses income", "income", "reimbursed", "1a", 100, False),
    ("4500", "Refunds & allowances to clients", "income", "returns", "1b", 100, False),
    ("4800", "Interest income", "income", "interest_income", "K4", 100, False),
    ("4900", "Other income", "income", "other_income", "5", 100, False),
    ("5000", "Cost of goods sold", "expense", "cogs", "2", 100, False),
    ("6000", "Officer compensation", "expense", "officer_comp", "7", 100, True),
    ("6010", "Salaries & wages", "expense", "wages", "8", 100, True),
    ("6020", "Payroll taxes (employer)", "expense", "payroll_tax", "12", 100, True),
    ("6040", "Officer health insurance (2% shareholders)", "expense", "officer_health", "7", 100, False),
    ("6100", "Repairs & maintenance", "expense", "repairs", "9", 100, False),
    ("6150", "Bad debts", "expense", "bad_debts", "10", 100, False),
    ("6200", "Rent", "expense", "rent", "11", 100, False),
    ("6250", "Taxes & licenses", "expense", "taxes_licenses", "12", 100, False),
    ("6300", "Interest expense", "expense", "interest", "13", 100, False),
    ("6350", "Depreciation", "expense", "depreciation", "14", 100, False),
    ("6400", "Advertising & marketing", "expense", "advertising", "16", 100, False),
    ("6450", "Retirement plan contributions (employer)", "expense", "retirement", "17", 100, False),
    ("6500", "Employee benefit programs", "expense", "benefits", "18", 100, False),
    ("6600", "Software & subscriptions", "expense", "software", "19", 100, False),
    ("6610", "Legal & professional fees", "expense", "professional", "19", 100, False),
    ("6620", "Office supplies & expenses", "expense", "office", "19", 100, False),
    ("6630", "Phone & internet", "expense", "phone_internet", "19", 100, False),
    ("6640", "Insurance (business)", "expense", "insurance", "19", 100, False),
    ("6650", "Travel", "expense", "travel", "19", 100, False),
    ("6660", "Business meals (50% deductible)", "expense", "meals", "19", 50, False),
    ("6670", "Bank & merchant fees", "expense", "bank_fees", "19", 100, False),
    ("6680", "Contract labor (1099)", "expense", "contractors", "19", 100, False),
    ("6690", "Other expenses", "expense", "other", "19", 100, False),
    ("6800", "Charitable contributions", "expense", "charity", "K12a", 100, False),
    ("6900", "Penalties & nondeductible", "expense", "nondeductible", "K16c", 100, False),
]


def seed_accounts(db: Session) -> None:
    existing = {a.code for a in db.scalars(select(Account)) if a.code}
    numbers = {a.number for a in db.scalars(select(Account))}
    for number, name, typ, code, line, pct, system in DEFAULT_ACCOUNTS:
        if code in existing:
            continue
        while number in numbers:  # user took the number for something else
            number = str(int(number) + 1)
        db.add(Account(number=number, name=name, type=typ, code=code, tax_line=line, deductible_pct=pct,
                       is_system=system, active=True))
        numbers.add(number)
    db.flush()


def ensure_bank_gl(db: Session, bank: BankAccount) -> Account:
    """Every bank account has its own asset account (1000-1049)."""
    if bank.gl_account_id:
        return db.get(Account, bank.gl_account_id)
    used = {a.number for a in db.scalars(select(Account))}
    n = 1000
    while str(n) in used:
        n += 1
    acct = Account(number=str(n), name=bank.label + (f" ({bank.institution})" if bank.institution else ""),
                   type="asset", tax_line="L1", is_system=True)
    db.add(acct)
    db.flush()
    bank.gl_account_id = acct.id
    return acct


def by_code(db: Session, code: str) -> Account:
    acct = db.scalar(select(Account).where(Account.code == code))
    if acct is None:
        raise LookupError(f"No account with code {code!r}")
    return acct


def migrate(engine) -> None:
    """Add columns introduced after a database was created (SQLite ALTER TABLE ADD COLUMN)."""
    from .db import Base

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


def backfill(db: Session) -> None:
    seed_accounts(db)
    for e in db.scalars(select(Expense).where(Expense.account_id.is_(None))):
        acct = db.scalar(select(Account).where(Account.code == (e.category or "other"))) or by_code(db, "other")
        e.account_id = acct.id
    for b in db.scalars(select(BankAccount).where(BankAccount.gl_account_id.is_(None))):
        ensure_bank_gl(db, b)
    db.commit()
