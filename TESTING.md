# Testing checklist

Start with the demo company (`start-mac.command demo` / `start-windows.bat demo`, or
`python -m timeentry --demo`), then repeat with your real data (`python -m timeentry`).

## Time & invoices
- [ ] Log time as `1.5`, `1:30` and `45m`; start and stop a timer.
- [ ] Invoices → New invoice → pick a client → create from unbilled time; add a fixed-fee line.
- [ ] Print / save the invoice as PDF and check it looks like something you'd send.
- [ ] Mark as sent, record a partial payment, then the rest. It should show as paid.

## Payroll
- [ ] Enter both owners' real W-4s; create a pay run and compare withholding to your last QuickBooks paystub.
- [ ] Enter your 2026 QuickBooks payrolls as historical pay runs; check Taxes → Form 941 Q1–Q3 against what you filed.
- [ ] Check W-2 totals (Taxes → W-2) against QuickBooks' year-to-date payroll summary.

## Bank (Chase)
- [ ] In Chase: Download account activity → Quicken (QFX) → import on the Bank page. Import it again; nothing should duplicate.
- [ ] Categorize a few transactions; undo one; check it appears in Expenses / on the invoice.
- [ ] Reconcile against your last Chase statement (ending date + balance). The difference should reach $0.00.

## Books & taxes
- [ ] Books → Profit & loss for 2026: compare with QuickBooks' P&L (cash basis).
- [ ] Balance sheet balances (no red warning); Trial balance debits = credits.
- [ ] Add an account (e.g. 6605 Cloud hosting, 1120-S line 19) and use it.
- [ ] Taxes → 1120-S: does it match what your CPA would expect?

## Safety
- [ ] Settings → Download backup works. Keep the file somewhere safe (it contains SSNs).

Write down anything that looks wrong or confusing; that's what we fix next.
