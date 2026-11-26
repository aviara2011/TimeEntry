"""Federal income tax withholding: IRS Pub. 15-T Worksheet 1A
(percentage method for automated payroll systems, Form W-4 2020 or later)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..money import round_cents
from .tables import params_for, withholding_table


@dataclass
class W4:
    filing_status: str = "single"        # single | mfj | hoh
    step2_checked: bool = False          # multiple jobs / spouse works
    step3_credits: int = 0               # annual dependent credits (cents)
    step4a_other_income: int = 0         # annual (cents)
    step4b_deductions: int = 0           # annual (cents)
    step4c_extra: int = 0                # extra withholding per pay period (cents)
    exempt: bool = False


def federal_withholding(wages: int, periods: int, w4: W4, year: int) -> int:
    """Withholding (cents) for one pay period of `wages` cents."""
    if w4.exempt or wages <= 0:
        return 0
    # Step 1: adjusted annual wage amount
    line_1c = Decimal(wages) * periods
    line_1e = line_1c + w4.step4a_other_income
    line_1g = 0
    if not w4.step2_checked:
        line_1g = params_for(year).line_1g[w4.filing_status]
    line_1h = w4.step4b_deductions + line_1g
    line_1i = max(Decimal(0), line_1e - line_1h)
    # Step 2: tentative withholding
    rows = withholding_table(year, w4.filing_status, w4.step2_checked)
    row = rows[0]
    for r in rows:
        if line_1i >= r.start:
            row = r
    annual = Decimal(row.base_tax) + (line_1i - row.start) * row.rate
    line_2h = annual / periods
    # Step 3: credits
    line_3b = Decimal(w4.step3_credits) / periods
    line_3c = max(Decimal(0), line_2h - line_3b)
    # Step 4: extra
    return round_cents(line_3c + w4.step4c_extra)
